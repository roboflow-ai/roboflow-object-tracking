import argparse
import time
from pathlib import Path

import clip

import cv2
import torch
import torch.backends.cudnn as cudnn
from numpy import random
import numpy as np

from models.experimental import attempt_load
from utils.datasets import LoadStreams, LoadImages
from utils.general import check_img_size, non_max_suppression, apply_classifier, scale_coords, xyxy2xywh, \
    strip_optimizer, set_logging, increment_path
from utils.plots import plot_one_box
from utils.torch_utils import select_device, load_classifier, time_synchronized
from utils.roboflow import predict_image

# deep sort imports
from deep_sort import preprocessing, nn_matching
from deep_sort.detection import Detection
from deep_sort.tracker import Tracker
from tools import generate_clip_detections as gdet


def detect(save_img=False):
    nms_max_overlap = opt.nms_max_overlap
    max_cosine_distance = opt.max_cosine_distance
    nn_budget = opt.nn_budget

    # initialize deep sort
    model_filename = "ViT-B/32"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, transform = clip.load(model_filename, device=device)
    encoder = gdet.create_box_encoder(model, transform, batch_size=1)
    # calculate cosine distance metric
    metric = nn_matching.NearestNeighborDistanceMetric(
        "cosine", max_cosine_distance, nn_budget)
    # initialize tracker
    tracker = Tracker(metric)

    source, weights, view_img, save_txt, imgsz = opt.source, opt.weights, opt.view_img, opt.save_txt, opt.img_size
    webcam = source.isnumeric() or source.endswith('.txt') or source.lower().startswith(
        ('rtsp://', 'rtmp://', 'http://'))

    # Directories
    save_dir = Path(increment_path(Path(opt.project) / opt.name,
                    exist_ok=opt.exist_ok))  # increment run
    (save_dir / 'labels' if save_txt else save_dir).mkdir(parents=True,
                                                          exist_ok=True)  # make dir

    # Initialize
    set_logging()
    device = select_device(opt.device)
    half = device.type != 'cpu'  # half precision only supported on CUDA

    # Load model
    model = attempt_load(weights, map_location=device)  # load FP32 model
    imgsz = check_img_size(imgsz, s=model.stride.max())  # check img_size
    if half:
        model.half()  # to FP16

    # Second-stage classifier
    classify = False
    if classify:
        modelc = load_classifier(name='resnet101', n=2)  # initialize
        modelc.load_state_dict(torch.load(
            'weights/resnet101.pt', map_location=device)['model']).to(device).eval()

    # Set Dataloader
    vid_path, vid_writer = None, None
    if webcam:
        view_img = True
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(source, img_size=imgsz)
    else:
        save_img = True
        dataset = LoadImages(source, img_size=imgsz)

    # Get names and colors
    names = model.module.names if hasattr(model, 'module') else model.names
    colors = [[random.randint(0, 255) for _ in range(3)] for _ in names]

    # Run inference
    t0 = time.time()
    img = torch.zeros((1, 3, imgsz, imgsz), device=device)  # init img
    # run once
    _ = model(img.half() if half else img) if device.type != 'cpu' else None

    frame_count = 0
    for path, img, im0s, vid_cap in dataset:
        img = torch.from_numpy(img).to(device)
        img = img.half() if half else img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)

        # Roboflow Inference
        pred = predict_image(img, opt.api_key, opt.url, frame_count)

        # Inference
        t1 = time_synchronized()
        pred = model(img, augment=opt.augment)[0]

        # Apply NMS
        pred = non_max_suppression(
            pred, opt.conf_thres, opt.iou_thres, classes=opt.classes, agnostic=opt.agnostic_nms)
        t2 = time_synchronized()

        # Apply Classifier
        if classify:
            pred = apply_classifier(pred, modelc, img, im0s)

        # Process detections
        for i, det in enumerate(pred):  # detections per image
            if webcam:  # batch_size >= 1
                p, s, im0, frame = path[i], '%g: ' % i, im0s[i].copy(
                ), dataset.count
            else:
                p, s, im0, frame = path, '', im0s, getattr(dataset, 'frame', 0)

            p = Path(p)  # to Path
            save_path = str(save_dir / p.name)  # img.jpg
            txt_path = str(save_dir / 'labels' / p.stem) + \
                ('' if dataset.mode == 'image' else f'_{frame}')  # img.txt
            s += '%gx%g ' % img.shape[2:]  # print string
            # normalization gain whwh
            gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]
            if len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(
                    img.shape[2:], det[:, :4], im0.shape).round()

                # Print results
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class
                    s += f'{n} {names[int(c)]}s, '  # add to string

                # Transform bboxes from tlbr to tlwh
                trans_bboxes = det[:, :4].clone()
                trans_bboxes[:, 2:] -= trans_bboxes[:, :2]
                bboxes = trans_bboxes[:, :4].cpu()
                confs = det[:, 4]
                class_nums = det[:, -1]

                # encode yolo detections and feed to tracker
                features = encoder(im0, bboxes)
                detections = [Detection(bbox, conf, class_num, feature) for bbox, conf, class_num, feature in zip(
                    bboxes, confs, class_nums, features)]

                # run non-maxima supression
                boxs = np.array([d.tlwh for d in detections])
                scores = np.array([d.confidence for d in detections])
                class_nums = np.array([d.class_num for d in detections])
                indices = preprocessing.non_max_suppression(
                    boxs, class_nums, nms_max_overlap, scores)
                detections = [detections[i] for i in indices]

                # Call the tracker
                tracker.predict()
                tracker.update(detections)

                # update tracks
                for track in tracker.tracks:
                    if not track.is_confirmed() or track.time_since_update > 1:
                        continue
                    xyxy = track.to_tlbr()
                    class_num = int(track.class_num)

                    if opt.info:
                        print("Tracker ID: {}, Class: {}, BBox Coords (xmin, ymin, xmax, ymax): {}".format(
                            str(track.track_id), class_num, (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))))

                    if save_txt:  # Write to file
                        xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)
                                          ) / gn).view(-1).tolist()  # normalized xywh
                        # label format
                        line = (class_num, *xywh,
                                conf) if opt.save_conf else (class_num, *xywh)
                        with open(txt_path + '.txt', 'a') as f:
                            f.write(('%g ' * len(line)).rstrip() % line + '\n')

                    if save_img or view_img:  # Add bbox to image
                        label = f'{names[class_num]} {track.track_id}'
                        plot_one_box(xyxy, im0, label=label,
                                     color=colors[class_num], line_thickness=3)

            # Print time (inference + NMS)
            print(f'{s}Done. ({t2 - t1:.3f}s)')

            # Stream results
            if view_img:
                cv2.imshow(str(p), im0)
                if cv2.waitKey(1) == ord('q'):  # q to quit
                    raise StopIteration

            # Save results (image with detections)
            if save_img:
                if dataset.mode == 'image':
                    cv2.imwrite(save_path, im0)
                else:  # 'video'
                    if vid_path != save_path:  # new video
                        vid_path = save_path
                        if isinstance(vid_writer, cv2.VideoWriter):
                            vid_writer.release()  # release previous video writer

                        fourcc = 'mp4v'  # output video codec
                        fps = vid_cap.get(cv2.CAP_PROP_FPS)
                        w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        vid_writer = cv2.VideoWriter(
                            save_path, cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
                    vid_writer.write(im0)

            frame_count = frame_count+1

    if save_txt or save_img:
        s = f"\n{len(list(save_dir.glob('labels/*.txt')))} labels saved to {save_dir / 'labels'}" if save_txt else ''
        print(f"Results saved to {save_dir}{s}")

    print(f'Done. ({time.time() - t0:.3f}s)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', nargs='+', type=str,
                        default='yolov5s.pt', help='model.pt path(s)')
    # file/folder, 0 for webcam
    parser.add_argument('--source', type=str,
                        default='data/images', help='source')
    parser.add_argument('--img-size', type=int, default=640,
                        help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float,
                        default=0.25, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float,
                        default=0.45, help='IOU threshold for NMS')
    parser.add_argument('--device', default='',
                        help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--view-img', action='store_true',
                        help='display results')
    parser.add_argument('--save-txt', action='store_true',
                        help='save results to *.txt')
    parser.add_argument('--save-conf', action='store_true',
                        help='save confidences in --save-txt labels')
    parser.add_argument('--classes', nargs='+', type=int,
                        help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--agnostic-nms', action='store_true',
                        help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true',
                        help='augmented inference')
    parser.add_argument('--update', action='store_true',
                        help='update all models')
    parser.add_argument('--project', default='runs/detect',
                        help='save results to project/name')
    parser.add_argument('--name', default='exp',
                        help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true',
                        help='existing project/name ok, do not increment')
    parser.add_argument('--nms_max_overlap', type=float, default=1.0,
                        help='Non-maxima suppression threshold: Maximum detection overlap.')
    parser.add_argument('--max_cosine_distance', type=float, default=0.4,
                        help='Gating threshold for cosine distance metric (object appearance).')
    parser.add_argument('--nn_budget', type=int, default=None,
                        help='Maximum size of the appearance descriptors allery. If None, no budget is enforced.')
    parser.add_argument('--api_key', type=int, default=None,
                        help='Roboflow API Key.')
    parser.add_argument('--url', type=int, default=None,
                        help='Roboflow Model URL.')
    parser.add_argument('--info', action='store_true',
                        help='Print debugging info.')
    opt = parser.parse_args()
    print(opt)

    with torch.no_grad():
        if opt.update:  # update all models (to fix SourceChangeWarning)
            for opt.weights in ['yolov5s.pt', 'yolov5m.pt', 'yolov5l.pt', 'yolov5x.pt']:
                detect()
                strip_optimizer(opt.weights)
        else:
            detect()
