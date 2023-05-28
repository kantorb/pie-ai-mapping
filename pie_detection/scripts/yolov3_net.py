#!/usr/bin/env python3

import pathlib
import os
import numpy as np
import tensorflow as tf
from tensorflow.keras import Model
from tensorflow.keras.layers import BatchNormalization, Conv2D, Input, ZeroPadding2D, LeakyReLU, UpSampling2D

from utils import load_class_names, output_boxes, draw_outputs, resize_image

class YoloV3Net():
    def __init__(self):
        self._dir = pathlib.Path(__file__).parent.resolve().parent.resolve()

        self.model_size = (416, 416, 3)
        self.num_classes = 80
        self.class_name = os.path.join(self._dir, 'data/coco.names')
        self.max_output_size = 40
        self.max_output_size_per_class= 20
        self.iou_threshold = 0.5
        self.confidence_threshold = 0.5

        self.weightfile = os.path.join(self._dir, 'weights/yolov3_weights.tf')
        self.cfgfile = os.path.join(self._dir, 'cfg/yolov3.cfg')
        self.img_filename = os.path.join(self._dir, 'data/images/test_charis.jpg')

        self.model = self.load_model(self.cfgfile, self.model_size, self.num_classes)
        self.model.load_weights(self.weightfile)

        self.class_names = load_class_names(self.class_name)

    def detect(self, image):
        image = np.array(image)
        image = tf.expand_dims(image, 0)
        resized_frame = resize_image(image, (self.model_size[0], self.model_size[1]))
        pred = self.model.predict(resized_frame)

        boxes, scores, classes, nums = output_boxes(
            pred,
            self.model_size,
            max_output_size=self.max_output_size,
            max_output_size_per_class=self.max_output_size_per_class,
            iou_threshold=self.iou_threshold,
            confidence_threshold=self.confidence_threshold)
        
        image = np.squeeze(image)
        img = draw_outputs(image, boxes, scores, classes, nums, self.class_names)
        return img

    def load_model(self, cfgfile, model_size, num_classes):
        blocks = self.parse_cfg(cfgfile)

        outputs = {}
        output_filters = []
        filters = []
        out_pred = []
        scale = 0

        inputs = input_image = Input(shape=model_size)
        inputs = inputs / 255.0

        for i, block in enumerate(blocks[1:]):
            # If it is a convolutional layer
            if (block["type"] == "convolutional"):

                activation = block["activation"]
                filters = int(block["filters"])
                kernel_size = int(block["size"])
                strides = int(block["stride"])

                if strides > 1:
                    inputs = ZeroPadding2D(((1, 0), (1, 0)))(inputs)

                inputs = Conv2D(filters,
                                kernel_size,
                                strides=strides,
                                padding='valid' if strides > 1 else 'same',
                                name='conv_' + str(i),
                                use_bias=False if ("batch_normalize" in block) else True)(inputs)

                if "batch_normalize" in block:
                    inputs = BatchNormalization(name='bnorm_' + str(i))(inputs)
                if activation == "leaky":
                    inputs = LeakyReLU(alpha=0.1, name='leaky_' + str(i))(inputs)

            elif (block["type"] == "upsample"):
                stride = int(block["stride"])
                inputs = UpSampling2D(stride)(inputs)

            # If it is a route layer
            elif (block["type"] == "route"):
                block["layers"] = block["layers"].split(',')
                start = int(block["layers"][0])

                if len(block["layers"]) > 1:
                    end = int(block["layers"][1]) - i
                    filters = output_filters[i + start] + output_filters[end]  # Index negatif :end - index
                    inputs = tf.concat([outputs[i + start], outputs[i + end]], axis=-1)
                else:
                    filters = output_filters[i + start]
                    inputs = outputs[i + start]

            elif block["type"] == "shortcut":
                from_ = int(block["from"])
                inputs = outputs[i - 1] + outputs[i + from_]

            # Yolo detection layer
            elif block["type"] == "yolo":

                mask = block["mask"].split(",")
                mask = [int(x) for x in mask]
                anchors = block["anchors"].split(",")
                anchors = [int(a) for a in anchors]
                anchors = [(anchors[i], anchors[i + 1]) for i in range(0, len(anchors), 2)]
                anchors = [anchors[i] for i in mask]

                n_anchors = len(anchors)

                out_shape = inputs.get_shape().as_list()

                inputs = tf.reshape(inputs, [-1, n_anchors * out_shape[1] * out_shape[2], \
                                            5 + num_classes])

                box_centers = inputs[:, :, 0:2]
                box_shapes = inputs[:, :, 2:4]
                confidence = inputs[:, :, 4:5]
                classes = inputs[:, :, 5:num_classes + 5]

                box_centers = tf.sigmoid(box_centers)
                confidence = tf.sigmoid(confidence)
                classes = tf.sigmoid(classes)

                anchors = tf.tile(anchors, [out_shape[1] * out_shape[2], 1])
                box_shapes = tf.exp(box_shapes) * tf.cast(anchors, dtype=tf.float32)

                x = tf.range(out_shape[1], dtype=tf.float32)
                y = tf.range(out_shape[2], dtype=tf.float32)

                cx, cy = tf.meshgrid(x, y)
                cx = tf.reshape(cx, (-1, 1))
                cy = tf.reshape(cy, (-1, 1))
                cxy = tf.concat([cx, cy], axis=-1)
                cxy = tf.tile(cxy, [1, n_anchors])
                cxy = tf.reshape(cxy, [1, -1, 2])

                strides = (input_image.shape[1] // out_shape[1], \
                        input_image.shape[2] // out_shape[2])
                box_centers = (box_centers + cxy) * strides

                prediction = tf.concat([box_centers, box_shapes, confidence, classes], axis=-1)

                if scale:
                    out_pred = tf.concat([out_pred, prediction], axis=1)
                else:
                    out_pred = prediction
                    scale = 1

            outputs[i] = inputs
            output_filters.append(filters)

        model = Model(input_image, out_pred)
        model.summary()
        return model

    def parse_cfg(self, cfgfile):
        with open(cfgfile, 'r') as file:
            lines = [line.rstrip('\n') for line in file if line != '\n' and line[0] != '#']
        holder = {}
        blocks = []
        for line in lines:
            if line[0] == '[':
                line = 'type=' + line[1:-1].rstrip()
                if len(holder) != 0:
                    blocks.append(holder)
                    holder = {}
            key, value = line.split("=")
            holder[key.rstrip()] = value.lstrip()
        blocks.append(holder)
        return blocks