# ==============================================================================
# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

import os, sys
from cntk import Axis, load_model
from cntk.io import MinibatchSource, ImageDeserializer, CTFDeserializer, StreamDefs, StreamDef
from cntk.io.transforms import scale
from cntk.ops import input_variable
from cntk.logging import TraceLevel
from htree_helper import get_tree_str
import numpy as np

abs_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(abs_path, ".."))
from utils.map.map_helpers import evaluate_detections
from utils.hierarchical_classification.hierarchical_classification_helper import HierarchyHelper

from H1_RunHierarchical import p, USE_HIERARCHICAL_CLASSIFICATION

# cls_map_str = abs_path.replace("\\", "/") + "/../../DataSets/Grocery/class_map.txt"  # TODO resolve mapping problem
HCH = HierarchyHelper(get_tree_str(p.datasetName, USE_HIERARCHICAL_CLASSIFICATION))

output_scale = (p.cntk_padWidth, p.cntk_padHeight)


def prepare_ground_truth_boxes(gtbs):
    """
    Creates an object that can be passed as the parameter "all_gt_infos" to "evaluate_detections" in map_helpers
    Parameters
    ----------
    gtbs - arraylike of shape (nr_of_images, nr_of_boxes, cords+original_label) where nr_of_boxes may be a dynamic axis

    Returns
    -------
    Object for parameter "all_gt_infos"
    """
    num_test_images = len(gtbs)
    classes = HCH.output_mapper.get_all_classes()  # list of classes with new labels and indexing # todo: check if __background__ is present!!!
    all_gt_infos = {key: [] for key in classes}
    for image_i in range(num_test_images):
        image_gtbs = np.copy(gtbs[image_i])
        coords = image_gtbs[:, 0:4]
        original_labels = image_gtbs[:, -1:]

        all_gt_boxes = []
        for gtb_i in range(len(image_gtbs)):
            label = int(original_labels[gtb_i][0])
            train_vector, _ = HCH.get_vectors_for_label_nr(label)
            reduced_vector = HCH.output_mapper.get_prediciton_vector(train_vector)  # remove lower backgrounds

            original_cls_name = HCH.cls_maps[0].getClass(label)
            for vector_i in range(1, len(reduced_vector)):
                if reduced_vector[vector_i] == 0: continue
                # else this label (vector_i) is active (either original or hypernym)

                current_class_name = classes[vector_i]
                if original_cls_name == current_class_name: original_cls_name = None

                lbox = np.concatenate([coords[gtb_i], [vector_i]], axis=0)
                lbox.shape = (1,) + lbox.shape
                all_gt_boxes.append(lbox)

            assert original_cls_name is None, "Original class label is not contained in mapped selection!"

        all_gt_boxes = np.concatenate(all_gt_boxes, axis=0)

        for cls_index, cls_name in enumerate(classes):
            if cls_index == 0: continue
            cls_gt_boxes = all_gt_boxes[np.where(all_gt_boxes[:, -1] == cls_index)]
            all_gt_infos[cls_name].append({'bbox': np.array(cls_gt_boxes),
                                           'difficult': [False] * len(cls_gt_boxes),
                                           'det': [False] * len(cls_gt_boxes)})

    return all_gt_infos


def prepare_predictions(outputs, roiss, num_classes):
    """
    prepares the prediction for the ap computation.
    :param outputs: list of outputs per Image of the network
    :param roiss: list of rois rewponsible for the predictions of above outputs.
    :param num_classes: the total number of classes
    :return: Prepared object for ap computation by utils.map.map_helpers
    """
    num_test_images = len(outputs)

    all_boxes = [[[] for _ in range(num_test_images)] for _ in range(num_classes)]

    for img_i in range(num_test_images):
        output = outputs[img_i]
        output.shape = output.shape[1:]
        rois = roiss[img_i]

        preds_for_img = []
        for roi_i in range(len(output)):
            pred_vector = output[roi_i]
            roi = rois[roi_i]

            processesed_vector = HCH.top_down_eval(pred_vector)
            reduced_p_vector = HCH.output_mapper.get_prediciton_vector(processesed_vector)

            assert len(reduced_p_vector) == num_classes
            for label_i in range(num_classes):
                if (reduced_p_vector[label_i] == 0): continue
                prediciton = np.concatenate([roi, [reduced_p_vector[label_i], label_i]])  # coords+score+label
                prediciton.shape = (1,) + prediciton.shape
                preds_for_img.append(prediciton)

        preds_for_img = np.concatenate(preds_for_img, axis=0)  # (nr_of_rois x 6) --> coords_scor_label

        for cls_j in range(1, num_classes):
            coords_score_label_for_cls = preds_for_img[np.where(preds_for_img[:, -1] == cls_j)]
            all_boxes[cls_j][img_i] = coords_score_label_for_cls[:, :-1].astype(np.float32, copy=False)

    return all_boxes


def create_mb_source(img_height, img_width, img_channels, n_rois):
    gt_dim = 5 * n_rois

    map_file = os.path.join(p.imgDir, "test_img_file.txt")
    gt_file = os.path.join(p.imgDir, "test_roi_file.txt")
    size_file = os.path.join(p.imgDir, "test_size_file.txt")

    # read images
    transforms = [scale(width=img_width, height=img_height, channels=img_channels,
                        scale_mode="pad", pad_value=114, interpolations='linear')]

    image_source = ImageDeserializer(map_file, StreamDefs(
        features=StreamDef(field='image', transforms=transforms)))

    # read rois and labels
    roi_source = CTFDeserializer(gt_file, StreamDefs(
        rois=StreamDef(field='rois', shape=gt_dim, is_sparse=False)))

    size_source = CTFDeserializer(size_file, StreamDefs(
        size=StreamDef(field='size', shape=2, is_sparse=False)))

    # define a composite reader
    return MinibatchSource([image_source, roi_source, size_source], max_samples=sys.maxsize, randomize=False,
                           trace_level=TraceLevel.Error)


def to_image_input_coordinates(coords, img_dims=None, relative_coord=False, centered_coords=False, img_input_dims=None,
                               needs_padding_adaption=True, is_absolut=False):
    """
    Converst the input coordinates to the coordinat type required for the prediction
    :param coords: the coord to be transformed
    :param img_dims: dimension of the image the coords are from. Not required if coordinates are not needing any adaption for the padding and are relative.
    :param relative_coord: whether the supplied coordinates are relative
    :param centered_coords: whether the supplied coordinarts are True:(center_x, center_y, width, heigth) or False:(left_bound, top_bound, right_bound, bottom_vound)
    :param img_input_dims: dimension of the detectors image input as tuple
    :param needs_padding_adaption: whether or not padding is used.
    :param is_absolut: whether or not the coordinate are absolute coordinates on the original image
    :return: transformed coords
    """
    if centered_coords:
        xy = coords[:, :2]
        wh_half = coords[:, 2:] / 2
        coords = np.concatenate([xy - wh_half, xy + wh_half], axis=1)

    # make coords relative
    if is_absolut:
        coords /= (img_dims[0], img_dims[1], img_dims[0], img_dims[1])
        relative_coord = True

    # applies padding transformation if required - restricted to sqare sized image inputs
    if needs_padding_adaption and img_dims is not None:
        if img_dims[0] > img_dims[1]:
            coords[:, [1, 3]] *= img_dims[1] / img_dims[0]
            coords[:, [1, 3]] += (1 - img_dims[1] / img_dims[0]) / 2
        elif img_dims[0] < img_dims[1]:
            coords[:, [0, 2]] *= img_dims[0] / img_dims[1]
            coords[:, [0, 2]] += (1 - img_dims[0] / img_dims[1]) / 2

    if relative_coord:
        coords *= img_input_dims + img_input_dims

    return coords


def eval_fast_rcnn_mAP(eval_model):
    classes = HCH.output_mapper.get_all_classes()
    num_test_images = 5
    num_classes = len(classes)
    num_channels = 3
    image_height = p.cntk_padHeight
    image_width = p.cntk_padWidth
    rois_per_image = p.cntk_nrRois

    image_input = input_variable((num_channels, image_height, image_width),
                                 dynamic_axes=[Axis.default_batch_axis()])  # , name=feature_node_name)
    gt_input = input_variable((rois_per_image * 5,))
    roi_input = input_variable((rois_per_image, 4), dynamic_axes=[Axis.default_batch_axis()])
    size_input = input_variable((2,))
    frcn_eval = eval_model(image_input, roi_input)

    # data_path = base_path
    minibatch_source = create_mb_source(image_height, image_width, num_channels, rois_per_image)

    input_map = {  # add real gtb
        image_input: minibatch_source.streams.features,
        gt_input: minibatch_source.streams.rois,
        size_input: minibatch_source.streams.size
    }

    all_raw_gt_boxes = []
    all_raw_outputs = []
    all_raw_rois = []
    all_raw_img_dims = []

    # evaluate test images and write netwrok output to file
    print("Evaluating Faster R-CNN model for %s images." % num_test_images)
    print(type(classes))
    for img_i in range(0, num_test_images):
        mb_data = minibatch_source.next_minibatch(1, input_map=input_map)

        img_size = mb_data[size_input].asarray()
        img_size.shape = (2,)
        all_raw_img_dims.append(img_size)

        # receives rel coords
        gt_data = mb_data[gt_input].asarray()
        gt_data.shape = (rois_per_image, 5)
        gt_data[:, 0:4] = to_image_input_coordinates(gt_data[:, 0:4],
                                                     img_dims=img_size,
                                                     relative_coord=False,
                                                     is_absolut=True,
                                                     centered_coords=False,
                                                     needs_padding_adaption=True,
                                                     img_input_dims=output_scale)

        all_gt_boxes = gt_data[np.where(gt_data[:, 4] != 0)]  # remove padded boxes!
        all_raw_gt_boxes.append(all_gt_boxes.copy())

        rois = np.copy(gt_data[:, :4])
        all_raw_rois.append(rois)

        output = frcn_eval.eval(
            {image_input: mb_data[image_input], roi_input: np.reshape(rois, roi_input.shape)})
        all_raw_outputs.append(output.copy())

    all_gt_infos = prepare_ground_truth_boxes(gtbs=all_raw_gt_boxes)
    all_boxes = prepare_predictions(all_raw_outputs, all_raw_rois, num_classes)

    aps = evaluate_detections(all_boxes, all_gt_infos, classes, apply_mms=True, use_07_metric=False)
    ap_list = []
    for class_name in classes:  # sorted(aps):
        if class_name == "__background__": continue
        ap_list += [aps[class_name]]
        print('AP for {:>15} = {:.6f}'.format(class_name, aps[class_name]))
    print('Mean AP = {:.6f}'.format(np.nanmean(ap_list)))

    return aps


if __name__ == '__main__':
    """
    Evaluates the Classification of the model created by the H1 script. Since only the classification is to be tested
    the roi_input is given the ground truth boxes. By this doing so it can be assured, that no issues due to bad region
    proposals is taken into account and the classification accurancy can be measured.
    """
    os.chdir(p.cntkFilesDir)
    model_path = os.path.join(abs_path, "Output", p.datasetName + "_hfrcn_py.model")

    # Train only is no model exists yet
    if os.path.exists(model_path):
        print("Loading existing model from %s" % model_path)
        trained_model = load_model(model_path)
    else:
        print("No trained model found! Start training now ...")
        import H1_RunHierarchical as h1

        trained_model = h1.create_and_safe_model(model_path)
        print("Stored trained model at %s" % model_path)

    # Evaluate the test set
    eval_fast_rcnn_mAP(trained_model)
