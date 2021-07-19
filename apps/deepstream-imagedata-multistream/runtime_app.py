import sys

sys.path.append('../')
import gi
import configparser

gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst
from gi.repository import GLib
from ctypes import *
import time
import sys
import math
import platform
from common.is_aarch_64 import is_aarch64
from common.FPS import GETFPS
import random
import numpy as np
import pyds
import cv2
import os
import os.path
from rtsp import rtsp_config as rc
from kafka_utils import *
from pymongo import MongoClient  #for error logs optional
from producer_config import config as producer_config
from confluent_kafka import Producer
from os import path
import collections

fps_streams = {}
frame_count = {}
streams_dict = {}
saved_count = {}
global PGIE_CLASS_ID_CARDBOARD
fps=5

MAX_NUM_SOURCES = rc.number_of_streams
g_num_sources = 0
g_source_id_list = [0] * MAX_NUM_SOURCES
g_eos_list = [False] * MAX_NUM_SOURCES
g_source_enabled = [False] * MAX_NUM_SOURCES
g_source_bin_list = [None] * MAX_NUM_SOURCES

MAX_DISPLAY_LEN = 64
PGIE_CLASS_ID_CARDBOARD = 0
MUXER_OUTPUT_WIDTH = 640
MUXER_OUTPUT_HEIGHT = 480
MUXER_BATCH_TIMEOUT_USEC = 4000000
TILED_OUTPUT_WIDTH = 640
TILED_OUTPUT_HEIGHT = 480
GST_CAPS_FEATURES_NVMM = "memory:NVMM"
pgie_classes_str = ["Cardboard"]
producer = Producer(producer_config)
MIN_CONFIDENCE = 0.5
MAX_CONFIDENCE = 1.0
global frame_counter

uri = ""

loop = None
pipeline = None
streammux = None
sink = None
pgie = None
nvvideoconvert = None
nvosd = None
tiler = None
tracker = None

def producer_frame(key = "V1", topic = "streams", partition = 0, time_stamp = 0, message = None):
    global producer
    if message is None:
        print("Illegal Message")
        return
    else:
        frames = serializeImg(message)
        producer.produce(
            topic=topic,
            value=frames,
            on_delivery=delivery_report,
            timestamp=time_stamp,
            partition=partition,
            key=key,
            headers={
                "video_name":str.encode("Key: " + key + "Partition: " + str(partition) + "Frames produced: " + str(time_stamp)),
                }
       )
        producer.poll(0.1)

def check_location(roi, detections):
    if int(detections[0]) in range(roi['xmin'], roi['xmax']) and int(detections[1]) in range(roi['xmin'] ,roi['xmax']):
        if int(detections[2]) in range(roi['ymin'], roi['ymax']) and int(detections[3]) in range(roi['ymin'], roi['ymax']):
            return True
        else:
            return False
    return False


# tiler_sink_pad_buffer_probe  will extract metadata received on tiler src pad
# and update params for drawing rectangle, object information etc.
def tiler_sink_pad_buffer_probe(pad, info, u_data):
    num_rects = 0
    frame_copy = None
    frame_number = 0
    speed = 'N/A'
    placeholder = cv2.imread('placeholder.jpg')


    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    # Retrieve batch metadata from the gst_buffer
    # Note that pyds.gst_buffer_get_nvds_batch_meta() expects the
    # C address of gst_buffer as input, which is obtained with hash(gst_buffer)
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            # Note that l_frame.data needs a cast to pyds.NvDsFrameMeta
            # The casting is done by pyds.NvDsFrameMeta.cast()
            # The casting also keeps ownership of the underlying memory
            # in the C code, so the Python garbage collector will leave
            # it alone.
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break
        n_frame = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
        frame_number = frame_meta.frame_num
        meta = streams_dict["stream{0}".format(frame_meta.source_id)]
        l_obj = frame_meta.obj_meta_list
        num_rects = frame_meta.num_obj_meta
        is_first_obj = True
        save_image = False
        obj_counter = {
            PGIE_CLASS_ID_CARDBOARD: 0,
        }
        while l_obj is not None:
            try:
                # Casting l_obj.data to pyds.NvDsObjectMeta
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break
            obj_counter[obj_meta.class_id] += 1
            # Periodically check for objects with borderline confidence value that may be false positive detections.
            # If such detections are found, annotate the frame with bboxes and confidence value.
            # Save the annotated frame to file.
            if (MIN_CONFIDENCE < obj_meta.confidence < MAX_CONFIDENCE):
                # Getting Image data using nvbufsurface
                # the input should be address of buffer and batch_id
                #n_frame = pyds.get_nvds_buf_surface(hash(gst_buffer), frame_meta.batch_id)
                confidence = '{0:.2f}'.format(obj_meta.confidence)
                rect_params = obj_meta.rect_params
                top = int(rect_params.top)
                left = int(rect_params.left)
                width = int(rect_params.width)
                height = int(rect_params.height)
                trk_id = obj_meta.object_id
                centroid = (width - top) // 2

                if check_location(meta["roi"], [top, left, top+width, left+height]) == True: 
                    if len(meta["boxes_speed_old"]) > 0 and (frame_number % (fps*5) == 0):
                            for key, value in meta["boxes_speed_old"].items():
                                if key == trk_id and top - value[0] <= 3 and \
                                top - value[0] >= -3 and left - value[1] <= 3 and \
                                left - value[1] >= -3 and width - value[2] <= 3 and \
                                width - value[2] >= -3 and height - value[3] <= 3 and \
                                height - value[3] >= -3:
                                    distance = centroid - ((value[2] - value[0])//2)
                                    time_elapsed = frame_number - meta["old_frame"]
                                    try:
                                        speed = distance // time_elapsed
                                        if speed == 0:
                                            meta["jammed_boxes"] += 1
                                    except ZeroDivisionError:
                                        continue

                    
                    if meta["jammed_boxes"] >= 2:
                            for key, value in meta["boxes_speed_old"].items():
                                if key == trk_id and top - value[0] <= 3 and \
                                top - value[0] >= -3 and left - value[1] <= 3 and \
                                left - value[1] >= -3 and width - value[2] <= 3 and \
                                width - value[2] >= -3 and height - value[3] <= 3 and \
                                height - value[3] >= -3:
                                    distance = centroid - ((value[2] - value[0])//2)
                                    time_elapsed = frame_number - meta["old_frame"]
                                    try:
                                        speed = distance // time_elapsed
                                        if speed == 0:
                                            continue
                                        else:
                                            meta["cleard_boxes"] += 1
                                    except ZeroDivisionError:
                                        continue
                                                        
                    if (frame_number % (fps+19) == 0):
                        meta["boxes_speed_old"][(trk_id)] = [top,left,width,height]
                        meta["old_frame"] = frame_number
                    
                    obj_name = pgie_classes_str[obj_meta.class_id]
                    n_frame = cv2.rectangle(n_frame, (left, top), (left + width, top + height), (0, 128, 255, 0), 2)
                    n_frame = cv2.putText(n_frame,str(obj_meta.object_id), (left, top),cv2.FONT_HERSHEY_DUPLEX, 0.5, (0, 0, 0, 0), 1)
                    
            '''frame_copy = np.array(n_frame, copy=True, order='C')
            # convert the array into cv2 default color format
            frame_copy = cv2.cvtColor(frame_copy, cv2.COLOR_RGBA2BGRA)'''

            try:
                l_obj = l_obj.next
            except StopIteration:
                break
        
        frame_copy = np.array(n_frame, copy=True, order='C')
            # convert the array into cv2 default color format
        frame_copy = cv2.cvtColor(frame_copy, cv2.COLOR_RGBA2BGRA)


        if meta["cleard_boxes"] >= 5:
           meta["jammed_boxes"] = 0
           meta["cleard_boxes"] = 0
           meta["boxes_speed_old"].clear()

        if meta["jammed_boxes"] >= 3 :
            meta["gflag_status"].append("jam")
        else:
            meta["gflag_status"].append("clear")

        if "jam" in meta["gflag_status"][-fps*15:]:
            meta["gflag_status"].append("jammed")
        
        
        #and "jammed" not in meta["gflag_status"][-fps*15:]:
        if len(meta["gflag_status"]) > fps*15 and "jammed" not in meta["gflag_status"][-fps*15:]:
            meta["gflag_status"].clear()

        cv2.rectangle(frame_copy, (meta["roi"]['xmin'], meta["roi"]['ymin']), (meta["roi"]['xmax'], meta["roi"]['ymax']), (0,0,0), 2)
        cv2.putText(frame_copy, f'Status: ', (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2, cv2.LINE_AA)
        cv2.putText(frame_copy, f'Visible: {obj_counter[PGIE_CLASS_ID_CARDBOARD]} ', (10, 80), cv2.FONT_HERSHEY_DUPLEX, 1, 0, 2, cv2.LINE_AA)
        if "jammed" in meta["gflag_status"][-fps*3:]:
            cv2.putText(frame_copy, f'Jammed', (120, 50),cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,204), 2, cv2.LINE_AA)
        else:
            cv2.putText(frame_copy, f'Cleared', (120, 50),cv2.FONT_HERSHEY_SIMPLEX, 1, (0,153, 0), 2, cv2.LINE_AA)

        if frame_copy is None:
            frame_copy = np.array(placeholder)
            frame_copy = cv2.cvtColor(frame_copy, cv2.COLOR_RGBA2BGRA)
        # Get frame rate through this probe
        fps_streams["stream{0}".format(frame_meta.source_id)].get_fps()
        producer_frame(key = "V"+str(frame_meta.source_id+1), topic = "stream" + str(frame_meta.source_id+1),
                time_stamp = frame_number, message = frame_copy)


        try:
            l_frame = l_frame.next
        except StopIteration:
            break
    return Gst.PadProbeReturn.OK

def cb_newpad(decodebin, pad, data):

    global streammux
    print("In cb_newpad\n")
    caps = pad.get_current_caps()
    gststruct = caps.get_structure(0)
    gstname = gststruct.get_name()

    # Need to check if the pad created by the decodebin is for video and not
    # audio.
    if (gstname.find("video") != -1):
        # Link the decodebin pad only if decodebin has picked nvidia
        # decoder plugin nvdec_*. We do this by checking if the pad caps contain
        # NVMM memory features.
        source_id = data
        pad_name = "sink_%u" % source_id
        print(pad_name)
        sinkpad = streammux.get_request_pad(pad_name)
        if pad.link(sinkpad) == Gst.PadLinkReturn.OK:
            print("Decodebin linked to pipeline")
        else:
            sys.stderr.write("Failed to link decodebin to pipeline\n")


def decodebin_child_added(child_proxy, Object, name, user_data):
    print("Decodebin child added:", name, "\n")
    if name.find("decodebin") != -1:
        Object.connect("child-added", decodebin_child_added, user_data)
    
    if(name.find("nvv4l2decoder") != -1):
        if (is_aarch64()):
            Object.set_property("enable-max-performance", True)
            Object.set_property("drop-frame-interval", 0)
            Object.set_property("num-extra-surfaces", 0)


def create_source_bin(index, uri):
    print("Creating source bin")

    global g_source_id_list

    # Create a source GstBin to abstract this bin's content from the rest of the
    # pipeline
    g_source_id_list[index] = index
    bin_name="source-bin-%02d" % index
    print(bin_name)
    print("Creating Stream",index)
    # Source element for reading from the uri.
    # We will use decodebin and let it figure out the container format of the
    # stream and the codec and plug the appropriate demux and decode plugins.
    bin=Gst.ElementFactory.make("uridecodebin", bin_name)
    if not bin:
        sys.stderr.write(" Unable to create uri decode bin \n")
    # We set the input uri to the source element
    bin.set_property("uri",uri)
    # Connect to the "pad-added" signal of the decodebin which generates a
    # callback once a new pad for raw data has been created by the decodebin
    bin.connect("pad-added",cb_newpad,g_source_id_list[index])
    bin.connect("child-added",decodebin_child_added,g_source_id_list[index])

    #Set status of the source to enabled
    g_source_enabled[index] = True
    return bin

def stop_release_source(source_id):
    global g_num_sources
    global g_source_bin_list
    global streammux
    global pipeline

    #Attempt to change status of source to be released 
    state_return = g_source_bin_list[source_id].set_state(Gst.State.NULL)

    if state_return == Gst.StateChangeReturn.SUCCESS:
        print("STATE CHANGE SUCCESS\n")
        pad_name = "sink_%u" % source_id
        print(pad_name)
        #Retrieve sink pad to be released
        sinkpad = streammux.get_static_pad(pad_name)
        #Send flush stop event to the sink pad, then release from the streammux
        sinkpad.send_event(Gst.Event.new_flush_stop(False))
        streammux.release_request_pad(sinkpad)
        print("STATE CHANGE SUCCESS\n")
        #Remove the source bin from the pipeline
        pipeline.remove(g_source_bin_list[source_id])
        source_id -= 1
        g_num_sources -= 1

    elif state_return == Gst.StateChangeReturn.FAILURE:
        print("STATE CHANGE FAILURE\n")
    
    elif state_return == Gst.StateChangeReturn.ASYNC:
        state_return = g_source_bin_list[source_id].get_state(Gst.CLOCK_TIME_NONE)
        pad_name = "sink_%u" % source_id
        print(pad_name)
        sinkpad = streammux.get_static_pad(pad_name)
        sinkpad.send_event(Gst.Event.new_flush_stop(False))
        streammux.release_request_pad(sinkpad)
        print("STATE CHANGE ASYNC\n")
        pipeline.remove(g_source_bin_list[source_id])
        source_id -= 1
        g_num_sources -= 1

def delete_sources(data):
    global loop
    global g_num_sources
    global g_eos_list
    global g_source_enabled

    print("Shobha")
    #First delete sources that have reached end of stream
    for source_id in range(MAX_NUM_SOURCES):
        if (g_eos_list[source_id] and g_source_enabled[source_id]):
            g_source_enabled[source_id] = False
            stop_release_source(source_id)

    #Quit if no sources remaining
    if (g_num_sources == 0):
        loop.quit()
        print("All sources stopped quitting")
        return False

    #Randomly choose an enabled source to delete
    source_id = random.randrange(0, MAX_NUM_SOURCES)
    while (not g_source_enabled[source_id]):
        source_id = random.randrange(0, MAX_NUM_SOURCES)
    #Disable the source
    g_source_enabled[source_id] = False
    #Release the source
    print("Calling Stop %d " % source_id)
    stop_release_source(source_id)

    #Quit if no sources remaining
    if (g_num_sources == 0):
        loop.quit()
        print("All sources stopped quitting")
        return False

    print("Successfully deleted", source_id, g_num_sources)    
    return True


def add_sources(data):
    global g_num_sources
    global g_source_enabled
    global g_source_bin_list

    print("kiran")
    source_id = g_num_sources
    #Randomly select an un-enabled source to add
    source_id = random.randrange(0, MAX_NUM_SOURCES)
    while (g_source_enabled[source_id]):
        source_id = random.randrange(0, MAX_NUM_SOURCES)

    #Enable the source
    g_source_enabled[source_id] = True

    print("Calling Start %d " % source_id)

    #Create a uridecode bin with the chosen source id
    source_bin = create_source_bin(source_id, uri)
    print("Called Start %d " % source_id)
    print("Created URI", uri)
    if (not source_bin):
        sys.stderr.write("Failed to create source bin. Exiting.")
        exit(1)
    
    #Add source bin to our list and to pipeline
    g_source_bin_list[source_id] = source_bin
    pipeline.add(source_bin)

    #Set state of source bin to playing
    state_return = g_source_bin_list[source_id].set_state(Gst.State.PLAYING)

    if state_return == Gst.StateChangeReturn.SUCCESS:
        print("STATE CHANGE SUCCESS\n")
        source_id += 1

    elif state_return == Gst.StateChangeReturn.FAILURE:
        print("STATE CHANGE FAILURE\n")
    
    elif state_return == Gst.StateChangeReturn.ASYNC:
        state_return = g_source_bin_list[source_id].get_state(Gst.CLOCK_TIME_NONE)
        source_id += 1

    elif state_return == Gst.StateChangeReturn.NO_PREROLL:
        print("STATE CHANGE NO PREROLL\n")

    g_num_sources += 1

    #If reached the maximum number of sources, delete sources every 10 seconds
    if (g_num_sources == MAX_NUM_SOURCES):
        GObject.timeout_add_seconds(10, delete_sources, g_source_bin_list)
        return False
    print("Successfully added", source_id, uri)    
    return True

def bus_call(bus, message, loop):
    global g_eos_list
    t = message.type
    if t == Gst.MessageType.EOS:
        sys.stdout.write("End-of-stream\n")
        loop.quit()
    elif t==Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        sys.stderr.write("Warning: %s: %s\n" % (err, debug))
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        sys.stderr.write("Error: %s: %s\n" % (err, debug))
        loop.quit()
    elif t == Gst.MessageType.ELEMENT:
        struct = message.get_structure()
        #Check for stream-eos message
        if struct is not None and struct.has_name("stream-eos"):
            parsed, stream_id = struct.get_uint("stream-id")
            if parsed:
                #Set eos status of stream to True, to be deleted in delete-sources
                print("Got EOS from stream %d" % stream_id)
                g_eos_list[stream_id] = True
    return True

def check_stream(i):
    source = cv2.VideoCapture(i)
    ret, _ = source.read()
    return ret, i

def if_stream(args):
    streams = 1
    for i in range(1, len(args)):
        if args[i].find("rtsp://") == 0 and all(check_stream(args[i])):
            fps_streams["stream{0}".format(i - 1)] = GETFPS(i)
            streams += 1
        elif args[i].find("rtsp://") == 0:
            while not all(check_stream(args[i])):
                print(f"\033[0;31m \nError @stream: Unable to Read Video Stream from one of the streams in {args}, Please check \n 1. Rtsp link is given properly \n 2. If Rtsp server is Up \n\033[0;0m")
                time.sleep(3)
                if all(check_stream(args[i])):
                    fps_streams["stream{0}".format(i - 1)] = GETFPS(i)
                    streams += 1
                    break
    print(f"\033[0;32mReady To stream\033[0;0m")
    return streams

def main(args, roi, streams):
    global g_num_sources
    global g_source_bin_list
    global uri

    global loop
    global pipeline
    global streammux
    global sink
    global pgie
    global nvvidconv
    global nvvidconv1
    global nvosd
    global tiler
    global tracker


    # Check input arguments
    if len(args) < 2:
        sys.stderr.write(f"\033[0;32mConfigError: Config File Not Found\033[0;0m")
        sys.exit(1)

    for i in range(0, len(args)):
        fps_streams["stream{0}".format(i)] = GETFPS(i)
        streams_dict["stream{0}".format(i)] = {"roi":roi[i] ,"gflag_status": [], "old_frame": 0, "cleard_boxes": 0, "jammed_boxes": 0, "boxes_speed_old": collections.defaultdict(int)}
    number_sources = len(args)

    # Standard GStreamer initialization
    GObject.threads_init()
    Gst.init(None)

    # Create gstreamer elements */
    # Create Pipeline element that will form a connection of other elements
    print("Creating Pipeline \n ")
    pipeline = Gst.Pipeline()
    is_live = False

    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")
    print("Creating streamux \n ")

    # Create nvstreammux instance to form batches from one or more sources.
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")

    pipeline.add(streammux)
    for i in range(number_sources):
        frame_count["stream_" + str(i)] = 0
        saved_count["stream_" + str(i)] = 0
        print("Creating source_bin ", i, " \n ")
        uri_name = args[i]
        if uri_name.find("rtsp://") == 0:
            is_live = True
        source_bin = create_source_bin(i, uri_name)
        if not source_bin:
            sys.stderr.write("Unable to create source bin \n")
        pipeline.add(source_bin)

    g_num_sources = number_sources    
    queue1=Gst.ElementFactory.make("queue","queue1")
    queue2=Gst.ElementFactory.make("queue","queue2")
    queue3=Gst.ElementFactory.make("queue","queue3")
    queue4=Gst.ElementFactory.make("queue","queue4")
    queue5=Gst.ElementFactory.make("queue","queue5")
    queue6=Gst.ElementFactory.make("queue","queue6")
    queue7=Gst.ElementFactory.make("queue","queue7")
    queue8=Gst.ElementFactory.make("queue","queue8")
    pipeline.add(queue1)
    pipeline.add(queue2)
    pipeline.add(queue3)
    pipeline.add(queue4)
    pipeline.add(queue5) 
    pipeline.add(queue6) 
    pipeline.add(queue7) 
    pipeline.add(queue8) 
    print("Creating Pgie \n ")
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")
    # Add nvvidconv1 and filter1 to convert the frames to RGBA
    # which is easier to work with in Python.
    
    tracker = Gst.ElementFactory.make("nvtracker", "tracker")
    if not tracker:
        sys.stderr.write(" Unable to create tracker \n")
    
    print("Creating nvvidconv1 \n ")
    nvvidconv1 = Gst.ElementFactory.make("nvvideoconvert", "convertor1")
    if not nvvidconv1:
        sys.stderr.write(" Unable to create nvvidconv1 \n")
    print("Creating filter1 \n ")
    caps1 = Gst.Caps.from_string("video/x-raw(memory:NVMM), format=RGBA")
    filter1 = Gst.ElementFactory.make("capsfilter", "filter1")
    if not filter1:
        sys.stderr.write(" Unable to get the caps filter1 \n")
    filter1.set_property("caps", caps1)
    print("Creating tiler \n ")
    tiler = Gst.ElementFactory.make("nvmultistreamtiler", "nvtiler")
    if not tiler:
        sys.stderr.write(" Unable to create tiler \n")
    print("Creating nvvidconv \n ")
    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")
    print("Creating nvosd \n ")
    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")
    if (is_aarch64()):
        print("Creating transform \n ")
        transform = Gst.ElementFactory.make("nvegltransform", "nvegl-transform")
        if not transform:
            sys.stderr.write(" Unable to create transform \n")

    print("Creating EGLSink \n")
    #sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer")
    sink = Gst.ElementFactory.make("fakesink", "nvvideo-renderer")
    if not sink:
        sys.stderr.write(" Unable to create egl sink \n")

    if is_live:
        print("Atleast one of the sources is live")
        streammux.set_property('live-source', 1)

    streammux.set_property('width', 640)
    streammux.set_property('height', 480)
    streammux.set_property('batch-size', number_sources)
    streammux.set_property('batched-push-timeout', 200000)
    pgie.set_property('config-file-path', "dstest_imagedata_config.txt")
    pgie_batch_size = pgie.get_property("batch-size")
    if (pgie_batch_size != number_sources):
        print("WARNING: Overriding infer-config batch-size", pgie_batch_size, " with number of sources ",
              number_sources, " \n")
        pgie.set_property("batch-size", number_sources)
    tiler_rows = int(math.sqrt(number_sources))
    tiler_columns = int(math.ceil((1.0 * number_sources) / tiler_rows))
    tiler.set_property("rows", tiler_rows)
    tiler.set_property("columns", tiler_columns)
    tiler.set_property("width", TILED_OUTPUT_WIDTH)
    tiler.set_property("height", TILED_OUTPUT_HEIGHT)

    sink.set_property("sync", 0)
    sink.set_property("qos",0)
    
    config = configparser.ConfigParser()
    config.read('ds_tracker.txt')
    config.sections()

    for key in config['tracker']:
        if key == 'tracker-width' :
            tracker_width = config.getint('tracker', key)
            tracker.set_property('tracker-width', tracker_width)
        if key == 'tracker-height' :
            tracker_height = config.getint('tracker', key)
            tracker.set_property('tracker-height', tracker_height)
        if key == 'gpu-id' :
            tracker_gpu_id = config.getint('tracker', key)
            tracker.set_property('gpu_id', tracker_gpu_id)
        if key == 'll-lib-file' :
            tracker_ll_lib_file = config.get('tracker', key)
            tracker.set_property('ll-lib-file', tracker_ll_lib_file)
        if key == 'll-config-file' :
            tracker_ll_config_file = config.get('tracker', key)
            tracker.set_property('ll-config-file', tracker_ll_config_file)
        if key == 'enable-batch-process' :
            tracker_enable_batch_process = config.getint('tracker', key)
            tracker.set_property('enable_batch_process', tracker_enable_batch_process)
        if key == 'enable-past-frame' :
            tracker_enable_past_frame = config.getint('tracker', key)
            tracker.set_property('enable_past_frame', tracker_enable_past_frame)


    if not is_aarch64():
        # Use CUDA unified memory in the pipeline so frames
        # can be easily accessed on CPU in Python.
        mem_type = int(pyds.NVBUF_MEM_CUDA_UNIFIED)
        streammux.set_property("nvbuf-memory-type", mem_type)
        nvvidconv.set_property("nvbuf-memory-type", mem_type)
        nvvidconv1.set_property("nvbuf-memory-type", mem_type)
        tiler.set_property("nvbuf-memory-type", mem_type)

    print("Adding elements to Pipeline \n")
    pipeline.add(pgie)
    pipeline.add(tracker)
    pipeline.add(filter1)
    pipeline.add(nvvidconv1)
    pipeline.add(tiler)
    pipeline.add(nvvidconv)
    pipeline.add(nvosd)
    if is_aarch64():
        pipeline.add(transform)
    pipeline.add(sink)

    print("Linking elements in the Pipeline \n")
    streammux.link(queue1)
    queue1.link(pgie)
    pgie.link(queue2)
    queue2.link(tracker)
    tracker.link(queue3)
    queue3.link(nvvidconv1)
    nvvidconv1.link(queue4)
    queue4.link(filter1)
    filter1.link(queue5)
    queue5.link(tiler)
    tiler.link(queue6)
    queue6.link(nvvidconv)
    nvvidconv.link(queue7)
    queue7.link(nvosd)
    if is_aarch64():
        nvosd.link(queue8)
        queue8.link(transform)
        transform.link(sink)
    else:
        nvosd.link(queue8)
        queue8.link(sink)
    
    # create an event loop and feed gstreamer bus mesages to it
    loop = GObject.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    tiler_sink_pad = tiler.get_static_pad("sink")
    if not tiler_sink_pad:
        sys.stderr.write(" Unable to get src pad \n")
    else:
        tiler_sink_pad.add_probe(Gst.PadProbeType.BUFFER, tiler_sink_pad_buffer_probe, 0)

    # List the sources
    print("Now playing...")
    for i, source in enumerate(args):
        print(i, ": ", source)

    print("Starting pipeline \n")
    # start play back and listed to events		
    pipeline.set_state(Gst.State.PLAYING)
    print("Before Sources", g_num_sources)
    #GObject.timeout_add_seconds(10, add_sources, g_source_bin_list)
    add_sources(g_source_bin_list)
    print("After Sources", g_num_sources)
    try:
        loop.run()
    except KeyboardInterrupt:
        print("Thank You, Good Byee")
        pipeline.set_state(Gst.State.NULL)
        exit()

    # cleanup
    pipeline.set_state(Gst.State.NULL)

def get_rtsp():
    args = []
    roi = []
    for i in range(rc.number_of_streams):
        args.append(rc.links[f"cam{i+1}"]["input_uri"])
        roi.append(rc.links[f"cam{i+1}"]["roi"])
    return args, roi


if __name__ == '__main__':
    arg_list, roi = get_rtsp()
    while True:
        try:
            streams = if_stream(arg_list)
            if streams:
                main(arg_list, roi, streams)
        except KeyboardInterrupt:
            print("Exitting....")
            break

