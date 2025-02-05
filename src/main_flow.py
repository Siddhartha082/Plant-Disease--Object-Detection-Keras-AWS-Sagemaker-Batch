from metaflow import FlowSpec, Parameter, step, current, batch, S3, environment
from custom_decorators import pip
import os
import time

# Loading environment variables
try:
    from dotenv import load_dotenv
    load_dotenv(verbose=True, dotenv_path='.env')
except ImportError:
    print("Env file not found!")


class main_flow(FlowSpec):
    TESTING = Parameter(
        name='testing',
        help='Determines if only one batch of data is used for testing purposes',
        default=True)

    SAGEMAKER_INSTANCE = Parameter(
        name='sagemaker_instance',
        help='AWS Instance to Power SageMaker Inference',
        default='ml.t2.medium')

    @step
    def start(self):
        """
        Start-up: check everything works or fail fast!
        """

        # Print out some debug info
        print("flow name: %s" % current.flow_name)
        print("run id: %s" % current.run_id)
        print("username: %s" % current.username)

        # Ensure user has set the appropriate env variables
        assert os.environ['WANDB_API_KEY']
        assert os.environ['WANDB_ENTITY']
        assert os.environ['WANDB_PROJECT']
        assert os.environ['S3_BUCKET_ADDRESS']
        assert os.environ['KAGGLE_USERNAME']
        assert os.environ['KAGGLE_KEY']
        assert os.environ['IAM_ROLE_SAGEMAKER']

        self.next(self.augment_data_train_model)

    @pip(libraries={'tensorflow': '2.15', 'keras-cv': '0.9.0', 'pycocotools': '2.0.7', 'wandb': '0.17.3'})
    # @batch(gpu=1, memory=8192, image='docker.io/tensorflow/tensorflow:latest-gpu', queue="job-queue-gpu-metaflow")
    @batch(memory=15360, queue="job-queue-metaflow") 
    @environment(vars={
        "S3_BUCKET_ADDRESS": os.getenv('S3_BUCKET_ADDRESS'),
        'WANDB_API_KEY': os.getenv('WANDB_API_KEY'),
        'WANDB_PROJECT': os.getenv('WANDB_PROJECT'),
        'WANDB_ENTITY': os.getenv('WANDB_ENTITY')})
    @step
    def augment_data_train_model(self):
        import tensorflow as tf
        from utils import create_model, parse_tfrecord_fn, dict_to_tuple
        import keras
        import wandb
        from wandb.integration.keras import WandbMetricsLogger, WandbModelCheckpoint
        import tarfile
        import keras_cv

        print("Num GPUs Available: ", len(tf.config.list_physical_devices('GPU')))

        print('Augmenting data')

        self.config = {
            "base_lr": 0.0001,
            "loss": "sparse_categorical_crossentropy",
            "epoch": 40,
            "batch_size": 32,
            "classification_loss": "focal",
            "box_loss": "smoothl1",
            "num_examples": 6,
            "bbox_format": "xyxy",
            "img_size": 416,
            "patience": 6,
            "iou_threshold": 0.2,
            "confidence_threshold": 0.6
        }

        def download_from_s3(s3_path, local_path):
            with S3() as s3:
                s3_blob = s3.get(s3_path).blob
            with open(local_path, 'wb') as f:
                f.write(s3_blob)

        s3_base_path = 's3://' + os.getenv('S3_BUCKET_ADDRESS') + '/raw_data/'
        self.train_tfrecord_file = 'train_leaves.tfrecord'
        self.val_tfrecord_file = 'val_test_leaves.tfrecord'
        download_from_s3(s3_base_path + 'leaves.tfrecord', self.train_tfrecord_file)
        download_from_s3(s3_base_path + 'test_leaves.tfrecord', self.val_tfrecord_file)

        train_dataset = tf.data.TFRecordDataset(self.train_tfrecord_file).map(parse_tfrecord_fn).ragged_batch(self.config['batch_size']).prefetch(buffer_size=tf.data.AUTOTUNE)
        val_dataset = tf.data.TFRecordDataset(self.val_tfrecord_file).map(parse_tfrecord_fn).ragged_batch(self.config['batch_size']).prefetch(buffer_size=tf.data.AUTOTUNE)

        # Testing with only one batch
        if self.TESTING:
            train_dataset = train_dataset.take(1)
            val_dataset = val_dataset.take(1)
            self.config["epoch"] = 2

        # Defining augmentations
        augmenter = keras.Sequential(
            [
                keras_cv.layers.JitteredResize(
                    target_size=(self.config['img_size'], self.config['img_size']), scale_factor=(0.8, 1.25), bounding_box_format=self.config['bbox_format']
                ),
                keras_cv.layers.RandomFlip(mode="horizontal_and_vertical", bounding_box_format=self.config['bbox_format']),
                keras_cv.layers.RandomRotation(factor=0.06, bounding_box_format=self.config['bbox_format']),
                keras_cv.layers.RandomSaturation(factor=(0.4, 0.6)),
                keras_cv.layers.RandomHue(factor=0.2, value_range=[0, 255]),
            ]
        )

        # Resize and pad images
        inference_resizing = keras_cv.layers.Resizing(
            self.config['img_size'], self.config['img_size'], pad_to_aspect_ratio=True, bounding_box_format=self.config['bbox_format']
        )

        # Augmenting training set/resizing validation set
        train_dataset = train_dataset.map(augmenter, num_parallel_calls=tf.data.AUTOTUNE)
        val_dataset = val_dataset.map(inference_resizing, num_parallel_calls=tf.data.AUTOTUNE)

        # Converting data into tuples suitable for training
        train_dataset = train_dataset.map(dict_to_tuple, num_parallel_calls=tf.data.AUTOTUNE)
        val_dataset = val_dataset.map(dict_to_tuple, num_parallel_calls=tf.data.AUTOTUNE)

        # Start a run, tracking hyperparameters
        run = wandb.init(
            project=os.getenv('WANDB_PROJECT'),
            entity=os.getenv('WANDB_ENTITY'),
            config=self.config
        )

        config = wandb.config
        self.run_id = run.id

        # Including a global_clipnorm is extremely important in object detection tasks
        checkpoint_path = "best-custom-model.weights.h5"

        callbacks_list = [
            # Conducting early stopping to stop after 2 epochs of non-improving validation loss
            keras.callbacks.EarlyStopping(
                monitor="val_loss",
                patience=self.config['patience'],
            ),

            # Saving the best model
            keras.callbacks.ModelCheckpoint(
                filepath=checkpoint_path,
                monitor="val_loss",
                save_best_only=True,
                save_weights_only=True
            ),

            # Custom metrics printing after each epoch
            tf.keras.callbacks.LambdaCallback(
                on_epoch_end=lambda epoch, logs:
                print(f"\nEpoch #{epoch + 1} \n" +
                      f"Loss: {logs['loss']:.4f} \n" +
                      f"mAP: {logs['MaP']:.4f} \n" +
                      f"Validation Loss: {logs['val_loss']:.4f} \n" +
                      f"Validation mAP: {logs['val_MaP']:.4f} \n")
            ),
            WandbMetricsLogger(log_freq="epoch"),

            WandbModelCheckpoint("models")
        ]

        model = create_model(config=config)

        print('Beginning model training')
        model.fit(
            train_dataset,
            validation_data=val_dataset,
            epochs=config.epoch,
            callbacks=callbacks_list,
            verbose=0,
        )

        # Create model with the weights of the best model
        model = create_model(config=config)
        model.load_weights(checkpoint_path)

        self.model = {
            'model': model.to_json(),
            'model_weights': model.get_weights()
        }

        model_name = f"detection-model-{config.base_lr}/1"
        local_tar_name = f"model-{config.base_lr}.tar.gz"

        # Create necessary directories before saving the model
        os.makedirs(os.path.dirname(model_name), exist_ok=True)

        # Define the custom serving function
        @tf.function(input_signature=[tf.TensorSpec([None, 416, 416, 3], tf.float32)])
        def serving_fn(images):
            # Get raw predictions
            encoded_predictions = model(images, training=False)
            # Decode the predictions
            decoded_predictions = model.decode_predictions(encoded_predictions, images)
            # Return the processed predictions
            return {'boxes': decoded_predictions['boxes'], 'classes': decoded_predictions['classes']}

        # Export the model with the custom serving function
        tf.saved_model.save(model, model_name, signatures={'serving_default': serving_fn})

        # Zip keras folder to a single tar file
        with tarfile.open(local_tar_name, mode="w:gz") as _tar:
            _tar.add(model_name, recursive=True)
        # Metaflow nice s3 client needs a byte object for the put
        with open(local_tar_name, "rb") as in_file:
            data = in_file.read()
            with S3(run=self) as s3:
                url = s3.put(local_tar_name, data)
                # Print it out for debug purposes
                print("Model saved at: {}".format(url))
                # Save this path for downstream reference
                self.s3_path = url

        run.finish()

        self.next(self.evaluate_model)

    @step
    def evaluate_model(self):
        import wandb
        import tensorflow as tf
        from utils import class_mapping, parse_tfrecord_fn, convert_format_keras_to_wandb, create_model
        import keras_cv
        from keras_cv import bounding_box

        print('Evaluating model')

        run = wandb.init(
            project=os.getenv('WANDB_PROJECT'),
            entity=os.getenv('WANDB_ENTITY'),
            config=self.config,
            id=self.run_id,
            resume="allow"
        )

        config = wandb.config

        class_set = wandb.Classes([
            {'name': name, 'id': id} for id, name in class_mapping.items()
        ])

        # Setup a WandB Table object to hold our dataset
        table = wandb.Table(
            columns=["Ground Truth", "Predictions"]
        )

        # Resetting val dataset, removing augmentations
        val_dataset = tf.data.TFRecordDataset([self.val_tfrecord_file])
        val_dataset = val_dataset.map(parse_tfrecord_fn)

        model = create_model(config=config)
        model.set_weights(self.model['model_weights'])

        # Customizing non-max supression of model prediction. You may have to customize iou_threshold and confidence_threshold
        model.prediction_decoder = keras_cv.layers.MultiClassNonMaxSuppression(
            bounding_box_format=self.config['bbox_format'],
            from_logits=True,
            iou_threshold=self.config['iou_threshold'],
            confidence_threshold=self.config['confidence_threshold'],
        )

        for example in val_dataset.take(config.num_examples):
            image, bounding_box_dict = example["images"].numpy(), example["bounding_boxes"]
            boxes, classes = bounding_box_dict['boxes'].numpy(), bounding_box_dict['classes'].numpy()

            all_boxes = convert_format_keras_to_wandb(box_list=boxes, classes_list=classes)

            ground_truth_image = wandb.Image(
                image,
                classes=class_set,
                boxes={
                    "ground_truth": {
                        "box_data": all_boxes,
                        "class_labels": class_mapping,
                    }
                }
            )

            # Get image as a tensor, include a batch dimension
            image = example["images"]
            self.image = tf.expand_dims(image, axis=0)  # Shape: (1, 416, 416, 3)

            # Get predicted bounding boxes on image
            y_pred = model.predict(self.image)

            confidence = y_pred['confidence'][0]
            self.confidence = [conf for conf in confidence if conf != -1]

            self.y_pred = bounding_box.to_ragged(y_pred)
            self.boxes = self.y_pred['boxes']
            self.classes = self.y_pred['classes']

            # Convert the ragged tensor to a list of lists
            self.box_list = self.boxes.to_list()
            self.classes_list = self.classes.to_list()

            # Remove batch dimension
            self.box_list = self.box_list[0]
            self.classes_list = self.classes_list[0]

            if not self.box_list:
                print("No bounding boxes predicted for test image.")
                predicted_image = wandb.Image(
                    image
                )
            else:
                all_boxes = convert_format_keras_to_wandb(box_list=self.box_list,
                                                          classes_list=self.classes_list,
                                                          confidence_list=self.confidence)

                predicted_image = wandb.Image(
                    image,
                    classes=class_set,
                    boxes={
                        "ground_truth": {
                            "box_data": all_boxes,
                            "class_labels": class_mapping,
                        }
                    }
                )

            table.add_data(ground_truth_image, predicted_image)

        print("Logging table.")
        wandb.log({"Plant Disease Predictions": table})
        run.finish()

        self.next(self.deploy)

    @step
    def deploy(self):
        import tensorflow as tf
        from sagemaker.tensorflow import TensorFlowModel, TensorFlowPredictor
        from utils import parse_tfrecord_fn, dict_to_tuple
        import keras_cv
        from keras_cv import bounding_box

        print('Deploying model')
        # generate a signature for the endpoint, using timestamp as a convention
        ENDPOINT_NAME = f'detection-{int(round(time.time() * 1000))}-endpoint'
        # print out the name, so that we can use it when deploying our lambda
        print(f"\n\n================\nEndpoint name is: {ENDPOINT_NAME}\n\n")
        model = TensorFlowModel(
            model_data=self.s3_path,
            framework_version='2.14',
            role=os.environ['IAM_ROLE_SAGEMAKER'])

        predictor = model.deploy(
            initial_instance_count=1,
            instance_type=self.SAGEMAKER_INSTANCE,
            endpoint_name=ENDPOINT_NAME)

        # Uncomment this if you already have an endpoint up, copy and past correct endpoint name
        # predictor = TensorFlowPredictor(
        #     endpoint_name='detection-1720463605573-endpoint',
        # )

        # Get a sample image to test the endpoint
        test_image_dataset = tf.data.TFRecordDataset([self.train_tfrecord_file]).map(parse_tfrecord_fn)
        inference_resizing = keras_cv.layers.Resizing(
            self.config["img_size"], self.config["img_size"], pad_to_aspect_ratio=True, bounding_box_format=self.config["bbox_format"]
        )
        test_image_dataset = test_image_dataset.map(inference_resizing)
        test_image_dataset = test_image_dataset.map(dict_to_tuple)
        image, _ = next(iter(test_image_dataset.take(1)))

        # Add batch dimension
        image = tf.expand_dims(image, axis=0).numpy()
        input = {'instances': image}

        # Get prediction
        result = predictor.predict(input)
        self.result = result['predictions'][0]

        # Pull boxes and classes from dict
        self.boxes, self.classes = self.result['boxes'], self.result['classes']

        # Convert to tensors for processing in to_ragged
        self.boxes_tensor, self.classes_tensor = tf.convert_to_tensor(self.boxes, dtype=tf.float32), tf.convert_to_tensor(self.classes, dtype=tf.float32)
        self.tensor_dict = {
            "boxes": self.boxes_tensor,
            "classes": self.classes_tensor
        }

        # Converting from dense to ragged tensors
        self.y_pred = bounding_box.to_ragged(self.tensor_dict)
        self.boxes, self.classes = self.y_pred['boxes'], self.y_pred['classes']

        # Convert the ragged tensor to a list of lists
        self.box_list, self.classes_list = self.boxes.numpy().tolist(), self.classes.numpy().tolist()

        if not self.box_list:
            print("\n No bounding boxes predicted by Sagemaker endpoint.")
        else:
            # Remove batch dimension
            self.box_list, self.classes_list = self.box_list[0], self.classes_list[0]

            print(f"boxes type: {type(self.box_list)}")
            print(f"classes type: {type(self.classes_list)}")

            print(f"boxes: {self.box_list}")
            print(f"classes: {self.classes_list}")

        # print("Deleting endpoint now...")
        predictor.delete_endpoint()
        print("Endpoint deleted!")

        self.next(self.end)

    @step
    def end(self):
        """
        The final step!
        """

        print("All done. \n\n Congratulations! Plants around the world will thank you. \n")
        return


if __name__ == '__main__':
    main_flow()
