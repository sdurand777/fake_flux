
import sys
sys.path.append('droid_slam')

from tqdm import tqdm
import numpy as np
import torch
import lietorch
import cv2
import os

os.environ["WEBRTC_IP"] = "192.168.51.109"

import time
import argparse

from torch.multiprocessing import Process
from droid_slam.droid import Droid
import multiprocessing.resource_tracker as rt
import torch.nn.functional as F

# importer package pour le timestamp
from datetime import datetime, timedelta

import sys
import gi
import cv2
import numpy as np
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import queue
import threading
import time
import collections
import binascii
import re
 
# recuperer les directory vers les fichiers proto
current_dir = os.path.dirname(os.path.abspath(__file__))
python_driver_path = os.environ["HOME"] + "/ivm-proto/driver/python/cleaned"
sys.path.append(python_driver_path)

# Try to import the protobuf module
try:
    sys.path.append(os.environ["HOME"]+'/ivm-proto/gen/python/ivm_backend')
    import camera_pb2
    HAS_PROTOBUF = True
except ImportError:
    HAS_PROTOBUF = False

from backend_driver import BackEndDriver
from utils_driver import Response

gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')

class SRTStreamProcessor:
    def __init__(   self,
                    stream_timeout=10,  # Temps d'attente maximum sans frame avec filename
                    filename_detection_timeout=300  # Temps maximum d'attente avant de considérer l'absence de filename comme un problème
                    ):
        # Initialize GStreamer
        Gst.init(None)

          # Event to signal threads to stop
        self.stop_event = threading.Event()
        self.dict_test = {}
        self.frames = []
        self.frames_bis = []
        self.frame_three = []
        self.dict_frames = {}
        # Frame collection
        self.frame_queue = queue.Queue()
        self.frames_by_reduced_pts = collections.OrderedDict()
        self.lock = threading.Lock()
        # Frame collection
        self.image_queue = queue.Queue()
        # Flags et compteurs de stream
        self.filename_tracking_active = False
        self.first_filename_received = False
        self.last_filename_time = None
        self.last_filename_time_process_sample = None
        self.stream_timeout = stream_timeout
        self.filename_detection_timeout = filename_detection_timeout
        self.start_time = time.time()
        # Global PTS offset (for all branches)
        self.global_offset = None
        # Downsampling factor (precision in seconds)
        # Will be set dynamically based on framerate
        self.pts_precision = None
        # Set placeholder for fps detection
        self.fps_detected = False
        self.detected_fps_values = []
        # Number of channels for video and KLV
        # Error handling variables
        self.error_count = 0
        self.max_retries = 3
        self.last_error_time = 0
        self.num_channels = 2
        # Build the pipeline using parse_launch
        pipeline_str = (
            "tcpclientsrc host=192.168.51.110 port=6010 ! queue ! tsdemux name=mux program-number=1\n"
        )
        def on_pad_buffer(pad, info, user_data):
            buffer = info.get_buffer()
            pts = buffer.pts
            pts_time = pts / Gst.SECOND  # Convert to seconds
            #print(f"{user_data} PTS: {pts} ({pts_time:.3f} seconds)")
            return Gst.PadProbeReturn.OK
        
        # Dynamically add video and KLV branches based on num_channels
        for i in range(self.num_channels):
            #pipeline_str += f"mux. ! image/x-jpc ! queue ! decodebin ! videoconvert ! appsink name=video_sink_{i} emit-signals=true sync=false "
            pipeline_str += f"srtclientsrc latency=1000 uri=srt://192.168.51.110:60{(i + 1)*20} ! application/x-rtp,media=video,encoding-name=JPEG,payload=26 ! queue ! rtpjpegdepay name=depay{i} ! nvjpegdec ! tee name=t{i}\n t{i}. ! queue ! videoconvert ! video/x-raw,format=RGB ! appsink name=video_sink_{i} emit-signals=true sync=true\n"
            pipeline_str += f"mux. ! meta/x-klv ! queue ! appsink name=klv_sink_{i} emit-signals=true sync=false\n"
        
        # # pipeline to show
        # pipeline_str += "t0. ! queue ! videoconvert ! autovideosink\n"
        # pipeline_str += "t1. ! queue ! videoconvert ! autovideosink\n"

        print(f"Creating pipeline: {pipeline_str}")
        self.pipeline = Gst.parse_launch(pipeline_str)

        # Get the elements whose pads we want to monitor
        depay = self.pipeline.get_by_name("depay0")

        # Add probes to the src pad of rtpjpegdepay (before decoding)
        depay_src_pad = depay.get_static_pad("src")
        depay_src_pad.add_probe(Gst.PadProbeType.BUFFER, on_pad_buffer, "Before decoding")
        
        # Connect callbacks for all channels
        for i in range(self.num_channels):
            video_sink = self.pipeline.get_by_name(f"video_sink_{i}")
            video_sink.connect("new-sample", self.on_new_video_sample, f"video_{i}")
            
            klv_sink = self.pipeline.get_by_name(f"klv_sink_{i}")
            klv_sink.connect("new-sample", self.on_new_klv_sample, f"klv_{i}")
        
        # Start the frame processing thread
        self.running = True
        self.processor_thread = threading.Thread(target=self.process_frames)
        self.processor_thread.daemon = True
        self.processor_thread.start()
    
        # fill frame queue with frames from gstreamer
        self.gst_thread = threading.Thread(target=self.run)
        self.gst_thread.daemon = True
        self.gst_thread.start()

        # Ajouter une référence au main loop
        self.main_loop = None


    def restart_pipeline(self):
        """Attempt to restart the GStreamer pipeline with improved error handling."""
        print("Attempting to restart pipeline...", file=sys.stderr)
        
        try:
            # Stop current pipeline completely
            self.pipeline.set_state(Gst.State.NULL)
            
            # Small delay to ensure pipeline is fully stopped
            time.sleep(1)
            
            # Reinitialize key attributes
            self.global_offset = None
            self.fps_detected = False
            self.detected_fps_values = []
            self.pts_precision = None
            
            # Clear queues
            while not self.frame_queue.empty():
                try:
                    self.frame_queue.get_nowait()
                    self.frame_queue.task_done()
                except queue.Empty:
                    break
            
            # Clear frames_by_reduced_pts
            self.frames_by_reduced_pts.clear()
            
            # Build the pipeline using parse_launch
            pipeline_str = (
                "tcpclientsrc host=192.168.51.110 port=6010 ! queue ! tsdemux name=mux program-number=1\n"
            )
            def on_pad_buffer(pad, info, user_data):
                buffer = info.get_buffer()
                pts = buffer.pts
                pts_time = pts / Gst.SECOND  # Convert to seconds
                #print(f"{user_data} PTS: {pts} ({pts_time:.3f} seconds)")
                return Gst.PadProbeReturn.OK
            
            # Dynamically add video and KLV branches based on num_channels
            for i in range(self.num_channels):
                #pipeline_str += f"mux. ! image/x-jpc ! queue ! decodebin ! videoconvert ! appsink name=video_sink_{i} emit-signals=true sync=false "
                pipeline_str += f"srtclientsrc latency=1000 uri=srt://192.168.51.110:60{(i + 1)*20} ! application/x-rtp,media=video,encoding-name=JPEG,payload=26 ! queue ! rtpjpegdepay name=depay{i} ! nvjpegdec ! tee name=t{i}\n t{i}. ! queue ! videoconvert ! video/x-raw,format=RGB ! appsink name=video_sink_{i} emit-signals=true sync=true\n"
                pipeline_str += f"mux. ! meta/x-klv ! queue ! appsink name=klv_sink_{i} emit-signals=true sync=false\n"
            
            # # pipeline to show
            # pipeline_str += "t0. ! queue ! videoconvert ! autovideosink\n"
            # pipeline_str += "t1. ! queue ! videoconvert ! autovideosink\n"

            print(f"Creating pipeline: {pipeline_str}")
            self.pipeline = Gst.parse_launch(pipeline_str)

            # Get the elements whose pads we want to monitor
            depay = self.pipeline.get_by_name("depay0")

            # Add probes to the src pad of rtpjpegdepay (before decoding)
            depay_src_pad = depay.get_static_pad("src")
            depay_src_pad.add_probe(Gst.PadProbeType.BUFFER, on_pad_buffer, "Before decoding")
            
            # Connect callbacks for all channels
            for i in range(self.num_channels):
                video_sink = self.pipeline.get_by_name(f"video_sink_{i}")
                video_sink.connect("new-sample", self.on_new_video_sample, f"video_{i}")
                
                klv_sink = self.pipeline.get_by_name(f"klv_sink_{i}")
                klv_sink.connect("new-sample", self.on_new_klv_sample, f"klv_{i}")
 
            # Print and create the pipeline
            print(f"Creating pipeline: {pipeline_str}")
            self.pipeline = Gst.parse_launch(pipeline_str)
            
            # Reconnect bus watch
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.on_message, self.main_loop)
            
            # Connect callbacks for all channels
            for i in range(self.num_channels):
                video_sink = self.pipeline.get_by_name(f"video_sink_{i}")
                video_sink.connect("new-sample", self.on_new_video_sample, f"video_{i}")
                
                klv_sink = self.pipeline.get_by_name(f"klv_sink_{i}")
                klv_sink.connect("new-sample", self.on_new_klv_sample, f"klv_{i}")
            
            # Start pipeline
            ret = self.pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                print("Failed to restart pipeline", file=sys.stderr)
                return False
            
            # Reset error tracking
            self.error_count = 0
            
            return True

        except Exception as e:
            print(f"Error during pipeline restart: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            return False



    def detect_framerate_from_caps(self, caps_str):
        """Extract framerate from caps string and calculate precision."""
        # Try to find framerate in caps string
        fps_match = re.search(r'framerate=\(fraction\)(\d+)/(\d+)', caps_str)
        if fps_match:
            num = int(fps_match.group(1))
            denom = int(fps_match.group(2))
            if denom > 0:
                fps = num / denom
                # Store detected fps
                self.detected_fps_values.append(fps)
                
                # Only set precision once we have enough samples
                if len(self.detected_fps_values) >= 3:
                    # Use the most common fps value
                    fps_counter = collections.Counter(self.detected_fps_values)
                    most_common_fps = fps_counter.most_common(1)[0][0]
                    
                    # Set precision to 1/framerate (frame duration)
                    if most_common_fps > 0:
                        self.pts_precision = 1.0 / most_common_fps
                    else:
                        self.pts_precision = 0.25
                    # print(f"Detected framerate: {most_common_fps} fps")
                    # print(f"Setting PTS precision to: {self.pts_precision:.6f}s")
                    self.fps_detected = True
        
        # Default precision if no framerate found
        if self.pts_precision is None:
            self.pts_precision = 0.25  # Default value
    


    # parse_klv function used in on_new_klv_sample get ImageInfo protobuf message
    def parse_klv(self, buffer):
        """Parse KLV data and extract protobuf message."""
        # Map buffer for reading
        success, map_info = buffer.map(Gst.MapFlags.READ)
        if not success:
            return "Failed to map buffer for reading"
            
        try:
            # Extract KLV data
            klv_data = map_info.data
            
            # Parse basic KLV structure
            if len(klv_data) < 20:  # Need at least key (16) + length (4)
                return f"KLV data too short: {len(klv_data)} bytes"
                
            # Extract key, length, and value
            key = binascii.hexlify(klv_data[:16]).decode('ascii')
            length = int.from_bytes(klv_data[16:20], 'big')
            
            if len(klv_data) < 20 + length:
                return f"KLV value truncated. Expected {length} bytes, got {len(klv_data) - 20}"
                
            value = klv_data[20:20+length]
            
            # Try to parse protobuf if available
            if HAS_PROTOBUF:
                try:
                    image_info = camera_pb2.ImageInfo()
                    image_info.ParseFromString(value)
                    return str(image_info)
                except Exception as e:
                    return f"Error parsing protobuf: {e}"
            else:
                # Simple hex dump if protobuf not available
                value_hex = binascii.hexlify(value[:min(32, len(value))]).decode('ascii')
                return f"KLV: Key={key}, Length={length}, Value prefix={value_hex}..."
        
        finally:
            # free the buffer
            buffer.unmap(map_info)
    




   
    def calculate_reduced_pts(self, pts_time):
        """Calculate the reduced (downsampled) PTS for this frame using global offset."""
        if pts_time < 0:
            return -1  # Handle invalid PTS
            
        # Make sure we have a valid precision value
        if self.pts_precision is None:
            self.pts_precision = 0.25  # Default value
        
        # Calculate or update global offset
        int_chk = 0
        downsampled_pts = 0
        if self.global_offset is None:
            # First frame - calculate initial offset
            downsampled_pts = int(pts_time / self.pts_precision) * self.pts_precision
            int_chk = int(pts_time / self.pts_precision) * self.pts_precision
            offset = pts_time - downsampled_pts
            self.global_offset = offset
        else:
            # Update offset using running average (80% old, 20% new)
            downsampled_pts = round((pts_time-self.global_offset) / self.pts_precision) * self.pts_precision
            int_chk = int(pts_time / self.pts_precision) * self.pts_precision
            new_offset = pts_time - downsampled_pts
            current_offset = self.global_offset


            updated_offset = 0.8 * current_offset + 0.2* new_offset
            print(f"Current offset: {current_offset} Updated offset: {updated_offset:.6f}s")
            
            # Only update if the change is significant
            if abs(updated_offset - current_offset) > 0.01:
                self.global_offset = updated_offset
        
        # Apply offset to get aligned PTS
        aligned_pts = pts_time - self.global_offset
        
        # Downsample to get bucket key
        reduced_pts = round(aligned_pts / self.pts_precision) * self.pts_precision
        print(f"PTS: {pts_time:.6f}s | Offset: {self.global_offset} | Aligned: {aligned_pts:.6f}s | Reduced: {reduced_pts:.3f}s | int_chk: {int_chk:.6f}s")
        
        return reduced_pts
    



    def on_new_video_sample(self, appsink, branch_id):
        sample = appsink.emit("pull-sample")
        if sample:
            # Try to detect framerate from caps if not already detected
            if not self.fps_detected:
                caps = sample.get_caps()
                caps_str = caps.to_string()
                self.detect_framerate_from_caps(caps_str)
            
            return self.process_sample(sample, branch_id, "video")
        return Gst.FlowReturn.OK
    


    def on_new_klv_sample(self, appsink, branch_id):
        sample = appsink.emit("pull-sample")
        if sample:
            return self.process_sample(sample, branch_id, "klv")
        return Gst.FlowReturn.OK
    


    def process_sample(self, sample, branch_id, stream_type):
        buffer = sample.get_buffer()
        caps = sample.get_caps()
        
        pts = buffer.pts
        dts = buffer.dts
        duration = buffer.duration
        
        # Convert nanoseconds to more readable format
        pts_time = pts / Gst.SECOND if pts != Gst.CLOCK_TIME_NONE else -1
        dts_time = dts / Gst.SECOND if dts != Gst.CLOCK_TIME_NONE else -1
        duration_time = duration / Gst.SECOND if duration != Gst.CLOCK_TIME_NONE else -1
        
        # Calculate reduced PTS using global offset
        reduced_pts = self.calculate_reduced_pts(pts_time)
        
        # Parse KLV data if this is a KLV sample
        klv_info = None
        filename_current = None
        if stream_type == "klv":
            klv_info = self.parse_klv(buffer)
            # check if klv_has filename
            if "filename:" in klv_info:
                # print 100 times *
                # print("*"*100)
                # print("*"*100)
                # Extract filename using regex
                filename_match = re.search(r'filename:\s*"([^"]+)"', klv_info)
                # check if filename_match is not None and not already in frames
                if filename_match and filename_match.group(1) not in self.frames_bis:
                    self.frames_bis.append(filename_match.group(1))
                    filename_current = filename_match.group(1)

        # Create frame info
        frame_info = {
            'branch_id': branch_id,
            'stream_type': stream_type,
            'pts': pts,
            'pts_time': pts_time,
            'reduced_pts': reduced_pts,
            'dts': dts,
            'dts_time': dts_time,
            'duration': duration,
            'duration_time': duration_time,
            'caps': caps.to_string(),
            'buffer_size': buffer.get_size(),
            'arrival_time': time.time(),
            'klv_info': klv_info
        }
                # Pour les frames vidéo, on stocke les données brutes et les caps
        if stream_type == "video":
            try:
                # Extraire les données du buffer à l'avance
                success, map_info = buffer.map(Gst.MapFlags.READ)
                if success:
                    frame_info['bufferdata'] = bytes(map_info.data)  # Copie des données
                    frame_info['caps_struct'] = caps.get_structure(0)
                    buffer.unmap(map_info)
                    #print(f"Debug: Buffer data stored, size: {len(frame_info['buffer_data'])}")
            except Exception as e:
                print(f"Debug: Error storing buffer data: {e}")


        # Queue frame for processing
        self.frame_queue.put(frame_info)

        # # Si on suit les filenames
        # if self.filename_tracking_active:
        #     if filename_current:
        #         # On a reçu des frames avec filename
        #         self.last_filename_time_process_sample = time.time()

        #     # Vérifier le délai sans filename
        #     elif time.time() - self.last_filename_time_process_sample > 5:
        #         print(f"Aucun filename reçu depuis {self.stream_timeout} secondes. Arrêt du stream.")
        #         self.stop_event.set()

        return Gst.FlowReturn.OK
    

    def process_frames(self):
        # Initialiser les dictionnaires de suivi (à déclarer dans __init__ ou à initialiser ici)
        if not hasattr(self, 'logged_pts_received'):
            # Dictionnaire pour les PTS qui contiennent des frames avec filename
            self.logged_pts_received = {}      # PTS ayant au moins un filename
            self.filename_branches = {}         # Branches avec filename par PTS
            self.missing_logged_branches = {}   # Branches manquantes pour les PTS avec filename
            self.complete_logged_pts = {}       # PTS complets pour les frames avec filename
            self.video_frames = {}              # Frames vidéo par filename
            self.klv_frames = {}              # Frames vidéo par filename
        
        # Continuously process frames from the queue
        last_cleanup_time = time.time()
        
        while self.running and not self.stop_event.is_set():
            try:
                # Wait for frames with timeout
                frame_info = self.frame_queue.get(timeout=0.1)
                
                with self.lock:
                    # Get the reduced PTS key for grouping
                    reduced_pts = frame_info['reduced_pts']

                    # Flag pour indiquer si ce frame contient un filename
                    has_filename = False

                    if frame_info["stream_type"] ==  "video":
                        if reduced_pts not in self.video_frames:
                            self.video_frames[reduced_pts] = []
                        self.video_frames[reduced_pts].append(frame_info["branch_id"])
                        self.video_frames[reduced_pts].append(frame_info["pts_time"])
                    
                    if frame_info["stream_type"] ==  "klv":
                        if reduced_pts not in self.klv_frames:
                            self.klv_frames[reduced_pts] = []
                        self.klv_frames[reduced_pts].append(frame_info["branch_id"])
                        self.klv_frames[reduced_pts].append(frame_info["pts_time"])
 

                    # Récupération du filename dans klv_info si présent
                    if 'klv_info' in frame_info and frame_info['klv_info']:
                        if "filename:" in frame_info['klv_info']:
                            # Extract filename using regex
                            filename_match = re.search(r'filename:\s*"([^"]+)"', frame_info['klv_info'])
                            if filename_match:
                                filename = filename_match.group(1)
                                has_filename = True
                                self.frame_three.append(filename)
                                # fill a dict with filename and pts
                                if filename not in self.dict_frames:
                                    self.dict_frames[filename] = []
                                # append the pts to the filename
                                self.dict_frames[filename].append(frame_info['pts_time'])
                                self.dict_frames[filename].append(reduced_pts)
                                
                                # Tracker ce PTS comme contenant un filename
                                if reduced_pts not in self.logged_pts_received:
                                    self.logged_pts_received[reduced_pts] = True
                                    self.filename_branches[reduced_pts] = set()
                    
                    if reduced_pts < 0:
                        # Skip frames without valid PTS
                        continue
                    
                    # Create bucket structure if needed
                    if reduced_pts not in self.frames_by_reduced_pts:
                        self.frames_by_reduced_pts[reduced_pts] = {}
                    
                    # Store frame by branch ID
                    branch_id = frame_info['branch_id']
                    self.frames_by_reduced_pts[reduced_pts][branch_id] = frame_info
                    
                    # Si ce frame a un filename ou si le PTS est déjà tracké comme ayant un filename,
                    # enregistrer cette branche
                    if has_filename or reduced_pts in self.logged_pts_received:
                        if reduced_pts not in self.filename_branches:
                            self.filename_branches[reduced_pts] = set()
                        self.filename_branches[reduced_pts].add(branch_id)
                    
                    # Check if bucket is complete (has all channels)
                    bucket = self.frames_by_reduced_pts[reduced_pts]
                    
                    # Expected branches
                    expected_branches = set()
                    for i in range(self.num_channels):
                        expected_branches.add(f"video_{i}")
                        expected_branches.add(f"klv_{i}")
                    
                    is_complete = all(branch in bucket for branch in expected_branches)
                    
                    # Process complete buckets immediately
                    if is_complete:
                        # Convert dictionary to list of frames
                        frames = list(bucket.values())
                        self.print_frame_group(reduced_pts, frames, True)
                        del self.frames_by_reduced_pts[reduced_pts]
                        
                        # Si ce PTS est tracké comme ayant un filename, le marquer comme complet
                        if reduced_pts in self.logged_pts_received:
                            self.complete_logged_pts[reduced_pts] = True
                    elif reduced_pts in self.logged_pts_received:
                        # Pour les PTS avec filename, suivre les branches manquantes
                        missing = expected_branches - set(bucket.keys())
                        self.missing_logged_branches[reduced_pts] = missing
                
                self.frame_queue.task_done()
                
                # Periodic cleanup of old frames (every 2 seconds)
                current_time = time.time()
                if current_time - last_cleanup_time > 2.0:
                    self.cleanup_old_frames()
                    last_cleanup_time = current_time
                    
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                # Perform cleanup during idle time
                self.cleanup_old_frames()
            except Exception as e:
                print(f"Error processing frames: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                if self.stop_event.is_set():
                    break
 

    def cleanup_old_frames(self):
        """Clean up old frame buckets."""
        with self.lock:
            current_time = time.time()
            for pts_key, bucket in list(self.frames_by_reduced_pts.items()):
                if not bucket:
                    continue
                
                # Find oldest frame in this bucket
                oldest_time = min(frame['arrival_time'] for frame in bucket.values())
                
                # Process old buckets that have been waiting for a while
                if current_time - oldest_time > 1.0:
                    # Remove old buckets without processing
                    del self.frames_by_reduced_pts[pts_key]
            
            # Limit number of stored buckets
            if len(self.frames_by_reduced_pts) > 10:
                # Keep only the 10 most recent keys (by PTS value)
                oldest_keys = sorted(self.frames_by_reduced_pts.keys())[:len(self.frames_by_reduced_pts) - 10]
                for key in oldest_keys:
                    del self.frames_by_reduced_pts[key]
    


    def print_frame_group(self, pts_key, frames, complete=True):
       
        # Group frames by type and branch
        video_frames = {}
        klv_frames = {}
        
        for frame in frames:
            if frame['stream_type'] == 'video':
                branch_id = frame['branch_id']
                if branch_id not in video_frames:
                    video_frames[branch_id] = []
                video_frames[branch_id].append(frame)
            else:  # KLV
                branch_id = frame['branch_id']
                if branch_id not in klv_frames:
                    klv_frames[branch_id] = []
                klv_frames[branch_id].append(frame)
        
        frame_cv = []

        frame_names = []

        frames_left = []
        frames_right = []

        pts_list = []

        session_name = None

        for branch_id, branch_frames in sorted(klv_frames.items()):
            for i, frame in enumerate(sorted(branch_frames, key=lambda f: f['pts_time'])):
                if 'klv_info' in frame and frame['klv_info']:
                    #print(f"KLV Data: {frame['klv_info']}")
                    if "filename:" in frame['klv_info']:
                        # Extract filename using regex
                        filename_match = re.search(r'filename:\s*"([^"]+)"', frame['klv_info'])
                        session_match = re.search(r'session_name:\s*"([^"]+)"', frame['klv_info'])

                        if session_match:
                            session_name = session_match.group(1)

                        if filename_match:
                            filename = filename_match.group(1)
                            self.frames.append(filename)
                            frame_names.append(filename)
                            pts_list.append(frame['reduced_pts'])
                            # pts_list.append(frame['pts_time'])

                            # Première détection de filename
                            if not self.first_filename_received:
                                print("Premier filename détecté !")
                                self.first_filename_received = True
                                self.filename_tracking_active = True
                                self.last_filename_time = time.time()

                            # Extract branch number from KLV branch_id (e.g., 'klv_0' -> '0')
                            branch_num = branch_id.split('_')[1]
                            video_branch_id = f"video_{branch_num}"

                            if video_branch_id == "video_0" and "LRL" in filename:
                                closest_frame = video_frames[video_branch_id][0]
                            elif video_branch_id == "video_1" and "LRR" in filename:
                                closest_frame = video_frames[video_branch_id][0]
                            else:
                                print("No video frame found for KLV frame")
                                
                            # Process and display the frame if we have the buffer data
                            if 'bufferdata' in closest_frame:
                                # récupérer les données pour le format
                                structure = closest_frame['caps_struct']
                                width = structure.get_value('width') 
                                height = structure.get_value('height')
                                pixel_format = structure.get_value('format')
                                print(f"processing image: {width}x{height}, format: {pixel_format}")
                                # traiter selon le format de pixel
                                if pixel_format == 'RGB':
                                    channels = 3
                                    dtype = np.uint8
                                    frame_data = np.ndarray((height, width, channels), dtype=dtype, buffer=closest_frame['bufferdata'])
                                    frame_rgb = cv2.cvtColor(frame_data, cv2.COLOR_RGB2BGR)
                                    
                                    # append frame
                                    frame_cv.append(frame_rgb)

                                    # save image to disk in folder images
                                    cv2.imwrite(f"images/{filename}", frame_rgb)


                    else:
                        frame_names.append("No Image")
 
         # -- verification names
        # Regex pour extraire le nombre avant 'LRL' ou 'LRR'
        pattern = r"_(\d+)(LRL|LRR)\.JPG"

        print("frame_names : ", frame_names)

        match1 = re.search(pattern, frame_names[0])
        match2 = re.search(pattern, frame_names[1])

        if match1 and match2:
            number1, tag1 = match1.groups()
            number2, tag2 = match2.groups()

            if number1 == number2:
                print("------------- Match")
                # print pts list
                print("PTS List : ", pts_list)
                print("Frame Names : ", frame_names)
                if frame_cv:
                    result = np.concatenate(frame_cv, axis=1)
                    self.image_queue.put((result, frame_names, session_name))
            else:
                print("------------- No Match")
                print("PTS List : ", pts_list)
                print("Frame Names : ", frame_names)
        else:
            self.image_queue.put((None, frame_names, session_name))

        # # add names to queue
        # self.image_queue.put(frame_names)

        # Si on suit les filenames
        if self.filename_tracking_active:
            if frame_names and all(name != "No Image" for name in frame_names):
                # On a reçu des frames avec filename
                self.last_filename_time = time.time()

            # Vérifier le délai sans filename
            elif time.time() - self.last_filename_time > self.stream_timeout:
                print(f"Aucun filename reçu depuis {self.stream_timeout} secondes. Arrêt du stream.")
                self.stop_event.set()

    def stop_pipeline(self, loop):
        """Properly stop the pipeline and cleanup resources."""
        try:
            # Stop the pipeline
            self.pipeline.set_state(Gst.State.NULL)
            
            # Stop the main loop
            if loop and loop.is_running():
                loop.quit()
            
            # Signal other threads to stop
            self.running = False
            self.stop_event.set()
            
            # Close any open OpenCV windows
            cv2.destroyAllWindows()
            
            print("Pipeline and threads stopped cleanly.", file=sys.stderr)
        except Exception as e:
            print(f"Error during pipeline stop: {e}", file=sys.stderr)



    def run(self):
        # # Start playing gstreamer pipeline
        # ret = self.pipeline.set_state(Gst.State.PLAYING)
        #
        # # check if pipeline started successfully
        # if ret == Gst.StateChangeReturn.FAILURE:
        #     print("Unable to set the pipeline to the playing state.", file=sys.stderr)
        #     sys.exit(1)
        
        self.retry_delay = 1
        attempt = 0
        ret = None
        while ret == None or ret == Gst.StateChangeReturn.FAILURE:
            attempt += 1 
            try:
                # Tentative de démarrage du pipeline
                ret = self.pipeline.set_state(Gst.State.PLAYING)
                
                # Vérification du succès du démarrage
                if ret == Gst.StateChangeReturn.FAILURE:
                    print("Unable to set the pipeline to the playing state.", file=sys.stderr)
                    raise Exception("Impossible de démarrer le pipeline")
                
                # Si le pipeline démarre avec succès, sortir de la boucle
                print(f"Pipeline démarré avec succès à la tentative {attempt}")
                break
            
            except Exception as e:
                print(f"Tentative {attempt} échouée : {e}", file=sys.stderr)
                
                # Ne pas attendre après la dernière tentative
                print(f"Nouvelle tentative dans {self.retry_delay} secondes...", file=sys.stderr)
                time.sleep(self.retry_delay)
        
        # if ret == Gst.StateChangeReturn.FAILURE:
        #     # Si toutes les tentatives ont échoué
        #     print("Échec définitif du démarrage du pipeline après {} tentatives".format(self.max_retries), file=sys.stderr)
        #     sys.exit(1)

        # Create and start the main loop
        self.main_loop = GLib.MainLoop()        

        # Add bus watch
        bus = self.pipeline.get_bus()
        # add signal watch to use callback
        bus.add_signal_watch()
        # connect to message signal to detect errors
        bus.connect("message", self.on_message, self.main_loop)
        
        # Run the loop
        try:
            while not self.stop_event.is_set():

                context = self.main_loop.get_context()
                context.iteration(True)
                time.sleep(0.1)
        except Exception as e:
            print(f"Error in GStreamer thread: {e}", file=sys.stderr)
        finally:
            # Cleanup
            self.stop_pipeline(self.main_loop)


    def on_message(self, bus, message, loop):
        t = message.type
        
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            current_time = time.time()
            
            # Reset error count if last error was more than 5 minutes ago
            if current_time - self.last_error_time > 300:
                self.error_count = 0

            self.error_count += 1
            self.last_error_time = current_time
            
            print(f"Error: {err}, {debug}", file=sys.stderr)
            
            # Detailed error logging
            if "discont detected" in str(debug):
                print("Detected stream discontinuity", file=sys.stderr)
            elif "Required size greater than remaining size in buffer" in str(debug):
                print("Buffer size mismatch detected", file=sys.stderr)
            
            # Retry pipeline restart if within max retries
            if self.error_count <= self.max_retries:
                if self.restart_pipeline():
                    print(f"Pipeline restarted. Attempt {self.error_count} of {self.max_retries}")
                    return True
                else:
                    print("Pipeline restart failed", file=sys.stderr)
            
            # If max retries reached or restart failed
            print("Max retry attempts reached. Stopping stream.", file=sys.stderr)
            self.stop_event.set()
            loop.quit()
            return False
        
        elif t == Gst.MessageType.EOS:
            print("End of stream")
            self.stop_event.set()
            loop.quit()
        
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"Warning: {warn}, {debug}")
        
        return True




def show_image(image):
    image = image.permute(1, 2, 0).cpu().numpy()
    cv2.imshow('image', image / 255.0)
    cv2.waitKey(1)



def image_stream_stereo_online_300(img_left, img_right, image_size=[320, 512], stereo=False, stride=1):
    """ image generator """
    K_l = np.array([322.580, 0.0, 259.260, 0.0, 322.580, 184.882, 0.0, 0.0, 1.0]).reshape(3,3)
    d_l = np.array([-0.070162237, 0.07551153, 0.0012286149,  0.00099302817, -0.018171599])
    R_l = np.array([
0.9999956354796169, -0.002172438871054654, 0.002002381349442793,
 0.002175041160237588, 0.9999967917532834, -0.00129833704855268,
 -0.001999554367437393, 0.001302686643787701, 0.9999971523908654
    ]).reshape(3,3)
    P_l = np.array([
322.6092376708984, 0, 257.7363166809082, 0,
 0, 322.6092376708984, 186.6225147247314, 0,
 0, 0, 1, 0
        ]).reshape(3,4)
    map_l = cv2.initUndistortRectifyMap(K_l, d_l, R_l, P_l[:3,:3], (514, 376), cv2.CV_32F)
   
    #print("------------ Right pre rectification ------------------")
    K_r = np.array([
322.638671875, 0, 255.9466552734375,
 0, 322.638671875, 187.4475402832031,
 0, 0, 1
        ]).reshape(3,3)
    d_r = np.array([
        -0.070313379,
 0.071827024,
 0.0004486586,
 0.00070285366,
 -0.015095583
        ]).reshape(5)
    R_r = np.array([
0.9999984896881986, -0.001713768657967563, -0.0002891683050380818,
 0.001714143276046202, 0.9999976855080072, 0.001300265918105914,
 0.0002869392807828858, -0.001300759630204676, 0.9999991128447232
    ]).reshape(3,3)
    
    P_r = np.array([
322.6092376708984, 0, 257.7363166809082, 48.37263543147446,
 0, 322.6092376708984, 186.6225147247314, 0,
 0, 0, 1, 0
        ]).reshape(3,4)
    map_r = cv2.initUndistortRectifyMap(K_r, d_r, R_r, P_r[:3,:3], (514, 376), cv2.CV_32F)

    intrinsics_vec = [322.6092376708984, 322.6092376708984, 257.7363166809082, 186.6225147247314]
    ht0, wd0 = [376, 514]

    # read all png images in folder
    #print("------- image paths ------")
    images_left = img_left
    images_right = img_right
    images = [cv2.remap(images_left, map_l[0], map_l[1], interpolation=cv2.INTER_LINEAR)]
    images += [cv2.remap(images_right, map_r[0], map_r[1], interpolation=cv2.INTER_LINEAR)]
    images = torch.from_numpy(np.stack(images, 0))
    images = images.permute(0, 3, 1, 2).to("cuda", dtype=torch.float32)
    images = F.interpolate(images, image_size, mode="bilinear", align_corners=False)
        
    intrinsics = torch.as_tensor(intrinsics_vec).cuda()
    intrinsics[0] *= image_size[1] / wd0
    intrinsics[1] *= image_size[0] / ht0
    intrinsics[2] *= image_size[1] / wd0
    intrinsics[3] *= image_size[0] / ht0

    #yield images, intrinsics
    return images, intrinsics



def image_stream_stereo_online_100(img_left, img_right, image_size=[320, 512], stereo=False, stride=1):
    """ image generator """
    K_l = np.array([4.5968276977539062e+02, 0., 3.0727267456054688e+02, 0., 4.5968276977539062e+02, 2.4262467956542969e+02, 0., 0., 1. ]).reshape(3,3)
    d_l = np.array([-3.34751934e-01, 1.55787796e-01, 1.08754646e-03,2.89721975e-05, -4.33074757e-02 ])
    R_l = np.array([9.9999053790816017e-01, -2.3672478452004815e-03,
       -3.6496892728158687e-03, 2.3651886133940277e-03,
       9.9999704137840129e-01, -5.6843404811530139e-04,
       3.6510240990418963e-03, 5.5979646602964553e-04,
       9.9999317830220469e-01 
    ]).reshape(3,3)
    P_l = np.array([ 4.5990068054199219e+02, 0., 3.3639562416076660e+02, 0., 0.,
       4.5990068054199219e+02, 2.6883334159851074e+02, 0., 0., 0., 1.,
       0. 
        ]).reshape(3,4)
    map_l = cv2.initUndistortRectifyMap(K_l, d_l, R_l, P_l[:3,:3], (616, 514), cv2.CV_32F)
   
    print("------------ Right pre rectification ------------------")
    K_r = np.array([4.6011859130859375e+02, 0., 3.0329241943359375e+02, 0., 4.6011859130859375e+02, 2.4381889343261719e+02, 0., 0., 1. ]).reshape(3,3)
    d_r = np.array([-3.34759861e-01, 1.55759037e-01, 7.29110325e-04,
       1.10754154e-04, -4.32639048e-02 
        ]).reshape(5)
    R_r = np.array([9.9999679129872809e-01, -2.5332565953597990e-03,
       1.8083868698132711e-06, 2.5332551721412530e-03,
       9.9999663218813351e-01, 5.6411933458219384e-04,
       -3.2374398044074352e-06, -5.6411294338637344e-04,
       9.9999984088304039e-01 
    ]).reshape(3,3)
    
    P_r = np.array([ 4.5990068054199219e+02, 0., 3.3639562416076660e+02,
       5.9697221843133875e+01, 0., 4.5990068054199219e+02,
       2.6883334159851074e+02, 0., 0., 0., 1., 0. 
        ]).reshape(3,4)
    map_r = cv2.initUndistortRectifyMap(K_r, d_r, R_r, P_r[:3,:3], (616, 514), cv2.CV_32F)

    intrinsics_vec = [4.5990068054199219e+02, 4.5990068054199219e+02, 3.3639562416076660e+02, 2.6883334159851074e+02]
    ht0, wd0 = [376, 514]

    # read all png images in folder
    #print("------- image paths ------")
    images_left = cv2.resize(img_left,(616,514))
    images_right = cv2.resize(img_right,(616,514))
    images = [cv2.remap(images_left, map_l[0], map_l[1], interpolation=cv2.INTER_LINEAR)]
    images += [cv2.remap(images_right, map_r[0], map_r[1], interpolation=cv2.INTER_LINEAR)]
    images = torch.from_numpy(np.stack(images, 0))
    images = images.permute(0, 3, 1, 2).to("cuda", dtype=torch.float32)
    images = F.interpolate(images, image_size, mode="bilinear", align_corners=False)
        
    intrinsics = torch.as_tensor(intrinsics_vec).cuda()
    intrinsics[0] *= image_size[1] / wd0
    intrinsics[1] *= image_size[0] / ht0
    intrinsics[2] *= image_size[1] / wd0
    intrinsics[3] *= image_size[0] / ht0

    #yield images, intrinsics
    return images, intrinsics





def display_frame(frame):
    # post process image 
    h,w,ch = frame.shape
    img_left = frame[:, :w//2]
    img_right = frame[:, w//2:]
    hl,wl,chl = img_left.shape
    hr,wr,chr = img_right.shape
    cv2.imshow("frame left", img_left) 
    cv2.imshow("frame right", img_right) 
    if cv2.waitKey(1) & 0xFF == ord('q'):
        cv2.destroyAllWindows()


def driver_connexion(driver, system_driver, system_config):
    #driver = BackEndDriver("IHM - Linux", "192.168.51.110").camera_driver
    driver = BackEndDriver("IHM").camera_driver
    system_driver = BackEndDriver().system_driver
    system_config = system_driver.get_system_configuration()
    return driver, system_driver, system_config


class StreamConnectionManager:
    def __init__(self, driver):
        self.driver = driver
        self.recording_flag = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None
 
    def connect_stream(self):
        """
        Attempt to establish a stream connection with robust error handling
        """
        max_retries = 1
        retry_delay = 1  # seconds between retry attempts
 
        for attempt in range(max_retries):
            try:
                # Get recording information
                recording = self.driver.camera_driver.get_recording_infos()
                if recording.success:
                    print(f"Stream connection attempt {attempt + 1} successful")
                    return recording.value
                else:
                    print(f"Stream connection failed (Attempt {attempt + 1})")
            except Exception as e:
                print(f"Connection error: {e}")
            # Wait before retrying
            time.sleep(retry_delay)
        # If all attempts fail
        print("Failed to establish stream connection after multiple attempts")
        return None
 
    def process_stream(self, data_stream):
        """
        Process the data stream with improved error handling
        """
        try:
            for data in data_stream:
                if self.stop_event.is_set():
                    break
 
                if data.is_recording == 1:
                    self.recording_flag.set()
                    print("Recording ON")
                else:
                    self.recording_flag.clear()
                    print("Recording OFF")
        except Exception as e:
            print(f"Stream interrupted: {e}")
            self.recording_flag.clear()
        finally:
            print("Stream processing ended")
 
    def stream_thread_handler(self):
        """
        Continuously manage stream connection and processing
        """
        while not self.stop_event.is_set():
            try:
                # Attempt to connect to stream
                data_stream = self.connect_stream()
                if data_stream is not None:
                    print("Stream connected, processing data...")
                    self.process_stream(data_stream)
                else:
                    print("Could not establish stream. Retrying...")
                # Prevent tight loop
                time.sleep(2)
            except Exception as e:
                print(f"Unexpected error in stream thread: {e}")
                time.sleep(2)
 
    def start(self):
        """
        Start the stream connection and processing thread
        """
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.stream_thread_handler)
        self.thread.daemon = True  # Allow thread to be terminated when main program exits
        self.thread.start()
 
    def stop(self):
        """
        Stop the stream processing thread
        """
        self.stop_event.set()
        if self.thread:
            self.thread.join()
 



if __name__ == '__main__':

    # recuperation timestamp
    now = datetime.now()
    timestamp = now.strftime("%H:%M:%S.%f")
    print("SLAM -",timestamp,"- Run System Images Acquisition")

    parser = argparse.ArgumentParser()
    parser.add_argument("--imagedir", type=str, help="path to image directory")
    parser.add_argument("--calib", type=str, help="path to calibration file")
    parser.add_argument("--t0", default=0, type=int, help="starting frame")
    parser.add_argument("--stride", default=3, type=int, help="frame stride")

    parser.add_argument("--weights", default="droid.pth")
    #parser.add_argument("--buffer", type=int, default=512)
    parser.add_argument("--buffer", type=int, default=2048)
    parser.add_argument("--image_size", default=[240, 320])
    parser.add_argument("--disable_vis", action="store_true")

    parser.add_argument("--beta", type=float, default=0.3, help="weight for translation / rotation components of flow")
    parser.add_argument("--filter_thresh", type=float, default=2.4, help="how much motion before considering new keyframe")
    parser.add_argument("--warmup", type=int, default=8, help="number of warmup frames")
    parser.add_argument("--keyframe_thresh", type=float, default=4.0, help="threshold to create a new keyframe")
    parser.add_argument("--frontend_thresh", type=float, default=16.0, help="add edges between frames whithin this distance")
    parser.add_argument("--frontend_window", type=int, default=25, help="frontend optimization window")
    parser.add_argument("--frontend_radius", type=int, default=2, help="force edges between frames within radius")
    parser.add_argument("--frontend_nms", type=int, default=1, help="non-maximal supression of edges")

    parser.add_argument("--backend_thresh", type=float, default=22.0)
    parser.add_argument("--backend_radius", type=int, default=2)
    parser.add_argument("--backend_nms", type=int, default=3)
    parser.add_argument("--upsample", action="store_true")
    parser.add_argument("--reconstruction_path", help="path to saved reconstruction")
    args = parser.parse_args()

    args.stereo = True

    torch.multiprocessing.set_start_method('spawn')

    now = datetime.now()
    timestamp = now.strftime("%H:%M:%S.%f")
    print("SLAM -",timestamp,"- Device count :",torch.cuda.device_count())
    #print("device count : ", torch.cuda.device_count())

    droid = None

    # processor
    # Création du processeur SRT avec des paramètres personnalisés
    processor = SRTStreamProcessor(
        stream_timeout=5,   # Arrêter si plus de filename pendant 10 secondes 
        filename_detection_timeout=300  # Arrêter si aucun filename n'est détecté après 5 minutes
    )

    driver = None
    system_driver = None
    system_config = None
   
    #driver, system_driver, system_config = driver_connexion(driver, system_driver, system_config)
    driver = BackEndDriver("IHM")
    system_driver = driver.system_driver
    system_config = system_driver.get_system_configuration()

    hydro_series = None

    if system_config.success:
        config_infos = system_config.value
        print("config_infos")
        print(config_infos)
        hydro_series = config_infos.hydro_series

    print("System Type : ", hydro_series)

    # Create stream connection manager
    stream_manager = StreamConnectionManager(driver)

    # flag session eu lieu 
    flag_session = 0

    frames_left = []
    frames_right = []

    keyframe_list = []

    session_name = None

    t = 0 #timestamp
    # Le thread principal attend que le flag soit signalé
    kfnum = 0
    kfnum_new = 0
    last_frame_time = None  # Initialisé à None

    print("En attente des frames...")
    try:
        # timing
        last_frame_time = datetime.now()

        while not processor.stop_event.is_set():
            current_time = datetime.now()
            time.sleep(0.1)
            
            # Traitement des frames quand disponibles
            if not processor.image_queue.empty():
                # reinit time from new frame
                last_frame_time = current_time

                # return concat frames with log names
                frames, frame_names, session_name = processor.image_queue.get()

                # display frames
                #GLib.idle_add(display_frame, frames)
                #display_frame(frames)

                # Collecter les noms de frames uniquement après la détection du premier filename
                print("###################################################################")
                print(f"Frames reçues: {frame_names[0]}, {frame_names[1]}")
                print("###################################################################")

                if processor.filename_tracking_active:
                    if frame_names[0] != "No Image" and frame_names[1] != "No Image":
                        frames_left.append(frame_names[0])
                        frames_right.append(frame_names[1])

                if frames is not None:
                    # # post process image 
                    h,w,ch = frames.shape

                    print("h : ", h, " w : ", w)
                    img_left = frames[:, :w//2]
                    img_right = frames[:, w//2:]

                    if hydro_series == "hydro300":
                        image, intrinsics = image_stream_stereo_online_300(img_left, img_right)
                    elif hydro_series == "hydro100":
                        image, intrinsics = image_stream_stereo_online_100(img_left, img_right)
                    else:
                        print("System not recognised")

                    if droid is None:
                        args.image_size = [image.shape[2], image.shape[3]]
                        droid = Droid(args)

                    # tracking
                    kfnum_new = droid.track(t, image, intrinsics=intrinsics)

                    # increase timestamp
                    t += 1

                    print("Frame recordee ---- ", t)
                    print("KF number ---- ", kfnum_new)

                    # check kf number
                    if kfnum != kfnum_new:
                        kfnum = kfnum_new
                        keyframe_list.append(t)
                        # print kf num
                        now = datetime.now()
                        timestamp = now.strftime("%H:%M:%S.%f")
                        print("SLAM -",timestamp,"- number of kf :",kfnum)   

            else:
                # Vérifier le timeout UNIQUEMENT après la première frame
                if last_frame_time is not None:
                    current_time = datetime.now()
                    if (current_time - last_frame_time) > timedelta(seconds=60):
                        print("Aucune frame reçue depuis plus de 60 secondes. Arrêt du traitement.")
                        print("###################################################################")
                        print("###################################################################")
                        print("###################################################################")
                        print("###################################################################")
                        print("###################################################################")
                        print("###################################################################")
                        print("###################################################################")
                        print("###################################################################")
                        time.sleep(0.1)
                        break
                
                # Pause courte pour éviter de surcharger le CPU
                time.sleep(0.1)


    except KeyboardInterrupt:
        print("Interruption par l'utilisateur")

    except Exception as e:
        print(f"Erreur inattendue: {e}")
        import traceback
        traceback.print_exc()  # Afficher la stack trace complète

    finally:
        # Arrêt propre
        processor.stop_event.set()
        
        # Attente des threads
        processor.gst_thread.join(timeout=2)
        processor.processor_thread.join(timeout=2)

        print("Frames Gauche : ", len(frames_left))
        print("Frames Droite : ", len(frames_right))
        print("Keyframe list : ", len(keyframe_list))
        print(keyframe_list) 
        
        # recuperation des keyframes 
        #kf_left = [frames_left[i] for i in keyframe_list]

        kf_left = [frames_left[i] for i in keyframe_list if i < len(frames_left)]

        # Écriture dans un fichier .txt
        with open("kf.txt", "w") as f:
            for frame in kf_left:
                # Écriture de la ligne originale (avec LRL)
                f.write(frame + "\n")
        
                # Création et écriture de la version avec LRR
                frame_right = frame.replace("LRL", "LRR")
                f.write(frame_right + "\n")

        # recuperation du ply name
        if session_name is None :
            if (len(frames_left) == 0):
                ply_name = "fichier_session_corrompue"
            else:
                ply_name = frames_left[0].split("_")[0]
        else:
            ply_name = session_name

        now = datetime.now()
        timestamp = now.strftime("%H:%M:%S.%f")
        print("*************************************")
        print("SLAM -",timestamp,"- Fin Real Time Loop")   

        try:
            if droid is not None:
                traj_est = droid.terminate()
                # attente pour save le ply file proprement
                time.sleep(1)
            else:
                print("Droid is None")
        except:
            print("*************************************")
            print("SLAM -",timestamp,"- terminate failed")   
            os._exit(0)

        home_path = os.environ["HOME"]
        current_path = os.path.dirname(os.path.abspath(__file__))

        os.system("mv "+home_path+"/dossier_ply/pcdfinal.ply "+home_path+"/dossier_ply/"+ply_name+".ply")
        # creer fichier ply
        os.system("mv "+current_path+"/kf.txt "+home_path+"/dossier_ply/"+ply_name+".txt")

        now = datetime.now()
        timestamp = now.strftime("%H:%M:%S.%f")
        print("SLAM -",timestamp,"- End Global Bundle Adjustment")   

        # Arrêter proprement le pipeline et le thread GStreamer
        os._exit(0)

