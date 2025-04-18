#!/usr/bin/env python3
import os
import sys
import cv2
import time
import socket
import threading
import argparse
import numpy as np
import re
import struct
from datetime import datetime
import logging
import subprocess

import itertools

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GLib', '2.0')
    from gi.repository import Gst, GLib, GObject
except ImportError:
    print("Le module PyGObject (gi) n'est pas installé. Installation en cours...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyGObject"])
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GLib', '2.0')
    from gi.repository import Gst, GLib, GObject

# Initialisation de GStreamer
Gst.init(None)

# Configuration du logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class GstStreamGenerator:
    def __init__(self, 
                 image_folder, 
                 tcp_host="127.0.0.1",  # Localhost
                 srt_host="127.0.0.1",  # Localhost
                 srt_port_left=6020, 
                 srt_port_right=6040, 
                 tcp_port=6010,
                 frame_rate=4,
                 pattern_left="LRL",
                 pattern_right="LRR"):
        """
        Initialise le générateur de flux en local en utilisant GStreamer
        
        Args:
            image_folder: Dossier contenant les images à streamer
            tcp_host: Adresse IP pour le flux TCP (localhost)
            srt_host: Adresse IP pour les flux SRT (localhost)
            srt_port_left: Port pour le flux SRT gauche
            srt_port_right: Port pour le flux SRT droit
            tcp_port: Port pour le flux TCP (KLV)
            frame_rate: Images par seconde
            pattern_left: Motif pour identifier les images gauches
            pattern_right: Motif pour identifier les images droites
        """
        self.image_folder = image_folder
        self.tcp_host = tcp_host
        self.srt_host = srt_host
        self.srt_port_left = srt_port_left
        self.srt_port_right = srt_port_right
        self.tcp_port = tcp_port
        self.frame_rate = frame_rate
        self.pattern_left = pattern_left
        self.pattern_right = pattern_right
        
        self.frame_delay = 1.0 / frame_rate
        self.running = False
        self.session_name = f"TEST_SESSION_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Charger les images
        self.left_images = []
        self.right_images = []
        self._load_images()
        
        # Vérification de la disponibilité des images
        if not self.left_images or not self.right_images:
            raise ValueError(f"Impossible de trouver les images dans {image_folder}. "
                             f"Assurez-vous d'avoir des images avec les motifs {pattern_left} et {pattern_right}.")
        
        # Tri des images pour assurer une séquence cohérente
        self.left_images.sort()
        self.right_images.sort()
        
        logger.info(f"Images gauches trouvées: {len(self.left_images)}")
        logger.info(f"Images droites trouvées: {len(self.right_images)}")
        
        # Pipelines GStreamer
        self.pipelines = {"left": None, "right": None, "tcp": None}
        self.klv_src = None
        self.main_loop = None
        self.main_loop_thread = None
        
        # KLV sender thread
        self.klv_thread = None
    
    def _load_images(self):
        """Charge les images depuis le dossier spécifié"""
        if not os.path.exists(self.image_folder):
            raise FileNotFoundError(f"Le dossier {self.image_folder} n'existe pas")
            
        for filename in os.listdir(self.image_folder):
            if not filename.lower().endswith(('.png', '.jpg', '.jpeg', '.JPG')):
                continue
                
            if self.pattern_left in filename:
                self.left_images.append(os.path.join(self.image_folder, filename))
            elif self.pattern_right in filename:
                self.right_images.append(os.path.join(self.image_folder, filename))
    
    def _create_klv_packet(self, filename, timestamp):
        """
        Crée un paquet KLV avec les métadonnées de l'image
        
        Args:
            filename: Nom du fichier (sera inclus dans les données KLV)
            timestamp: Timestamp de l'image
            
        Returns:
            bytes: Contenu KLV formaté
        """
        # Création d'un message de métadonnées
        klv_content = f'image_index: 1\nfilename: "{os.path.basename(filename)}"\nsession_name: "{self.session_name}"\ntimestamp: {timestamp}\n'
        
        # Encodage en bytes
        klv_data = klv_content.encode('utf-8')
        
        return klv_data
    
    def _setup_tcp_pipeline(self):
        """Crée une pipeline GStreamer pour envoyer les données KLV en TCP au format compatible"""
        # pipeline_str = (
        #     f"appsrc name=klvsrc is-live=true block=false format=time do-timestamp=true "
        #     f"caps=meta/x-klv ! "
        #     f"queue ! mpegtsmux name=mux ! "
        #     f"tcpserversink host={self.tcp_host} port={self.tcp_port} recover-policy=keyframe sync=false"
        # )

        pipeline_str = (
            f"appsrc name=klvsrc is-live=true block=false format=time do-timestamp=true "
            f"caps=application/x-klv ! "  # Changement de meta/x-klv à application/x-klv
            f"klvparse ! queue ! "  # Ajouter un parseur KLV
            f"mpegtsmux name=mux ! "
            f"tcpserversink host={self.tcp_host} port={self.tcp_port} recover-policy=keyframe sync=false"
        )
        
        self.pipelines["tcp"] = Gst.parse_launch(pipeline_str)
        self.klv_src = self.pipelines["tcp"].get_by_name("klvsrc")
        
        # Configuration de l'appsrc pour KLV
        self.klv_src.set_property("format", Gst.Format.TIME)
        self.klv_src.set_property("is-live", True)
        self.klv_src.set_property("do-timestamp", True)
        
        # Bus pour le traitement des messages
        bus = self.pipelines["tcp"].get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message, "tcp")
        
        # Démarrer la pipeline
        ret = self.pipelines["tcp"].set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Impossible de démarrer la pipeline TCP KLV")
            
        logger.info(f"Pipeline TCP KLV démarrée sur {self.tcp_host}:{self.tcp_port}")
    
    def _send_klv_data(self, klv_data):
        """Envoie des données KLV via la pipeline GStreamer"""
        if not self.klv_src or not self.running:
            return False
            
        try:
            # Créer un buffer GStreamer avec les données KLV
            buffer = Gst.Buffer.new_allocate(None, len(klv_data), None)
            buffer.fill(0, klv_data)
            
            # Définir le timestamp et la durée
            current_time = Gst.util_get_timestamp()
            frame_duration = Gst.SECOND // int(self.frame_rate)
            
            buffer.pts = buffer.dts = current_time
            buffer.duration = frame_duration
            
            # Envoyer le buffer dans la pipeline
            ret = self.klv_src.emit("push-buffer", buffer)
            
            if ret != Gst.FlowReturn.OK:
                logger.warning(f"Erreur lors de l'envoi des données KLV: {ret}")
                return False
                
            return True
        except Exception as e:
            logger.error(f"Exception lors de l'envoi des données KLV: {e}")
            return False
    
    def _setup_gstreamer_pipeline(self, side):
        """Configure une pipeline GStreamer pour diffuser les images en SRT"""
        import cv2
        import threading
        import time
        import itertools
        from gi.repository import Gst, GLib

        images = self.left_images if side == "left" else self.right_images
        port = self.srt_port_left if side == "left" else self.srt_port_right

        if not images:
            raise ValueError(f"Aucune image trouvée pour le côté {side}")

        # Pipeline adaptée pour être compatible avec le récepteur
        pipeline_str = (
            f"appsrc name=src is-live=true block=false format=time do-timestamp=true "
            f"caps=image/jpeg,framerate={self.frame_rate}/1 ! "
            f"jpegparse ! tee name=t "

            # Branche SRT avec le bon format RTP pour être compatible avec le récepteur
            f"t. ! queue max-size-buffers=2 leaky=downstream ! "
            f"rtpjpegpay pt=26 ! "  # Payload type 26 comme attendu par le récepteur
            f"application/x-rtp,media=video,encoding-name=JPEG,payload=26 ! "
            f"srtserversink uri=srt://{self.srt_host}:{port}/ "
            f"wait-for-connection=false latency=1000 async=false "

            # Branche locale pour affichage
            f"t. ! queue max-size-buffers=2 leaky=downstream ! jpegdec ! "
            f"textoverlay name=overlay font-desc=\"Sans 24\" halignment=left valignment=top shaded-background=true ! "
            f"videoconvert ! autovideosink sync=false "
        )

        pipeline = Gst.parse_launch(pipeline_str)
        self.pipelines[side] = pipeline
        appsrc = pipeline.get_by_name("src")
        
        # Configuration supplémentaire de l'appsrc
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("do-timestamp", True)
        appsrc.set_property("min-latency", 0)
        appsrc.set_property("max-latency", 1000000000)  # 1 seconde en nanosecondes
        
        # Bus avec traitement d'erreurs amélioré
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message, side)

        # Variables pour la gestion des timestamps
        image_cycle = itertools.cycle(images)
        overlay = pipeline.get_by_name("overlay")
        frame_count = [0]  # Utiliser une liste pour permettre la modification dans la closure
        last_timestamp = [0]
        
        def on_need_data(src, length):
            if not self.running:
                return

            try:
                img_path = next(image_cycle)
                frame = cv2.imread(img_path)
                if frame is None:
                    logger.warning(f"[{side}] Image introuvable: {img_path}")
                    return

                # Redimensionner et encoder en JPEG
                frame = cv2.resize(frame, (640, 480))
                ret, jpeg_data = cv2.imencode('.jpg', frame)
                if not ret:
                    logger.warning(f"[{side}] Échec d'encodage JPEG")
                    return
                    
                jpeg_bytes = jpeg_data.tobytes()
                filename = os.path.basename(img_path)

                if overlay:
                    overlay.set_property("text", f"{filename} - Frame: {frame_count[0]}")

                # Créer un nouveau buffer
                buffer = Gst.Buffer.new_allocate(None, len(jpeg_bytes), None)
                buffer.fill(0, jpeg_bytes)
                
                # Gérer les timestamps avec précision
                current_time = Gst.util_get_timestamp()
                frame_duration = Gst.SECOND // int(self.frame_rate)
                
                # Assurer une progression logique du timestamp
                if last_timestamp[0] == 0:
                    last_timestamp[0] = current_time
                else:
                    last_timestamp[0] += frame_duration
                
                buffer.pts = buffer.dts = last_timestamp[0]
                buffer.duration = frame_duration
                
                # Pousser le buffer dans la pipeline
                ret = src.emit("push-buffer", buffer)
                
                if ret != Gst.FlowReturn.OK:
                    if ret == Gst.FlowReturn.EOS:
                        logger.warning(f"[{side}] EOS atteint")
                    else:
                        logger.warning(f"[{side}] Erreur push-buffer: {ret}")
                else:
                    frame_count[0] += 1
                    # Log à intervalles réguliers pour surveiller le flux
                    if frame_count[0] % 30 == 0:
                        logger.info(f"[{side}] {frame_count[0]} frames envoyées")
                    
            except Exception as e:
                logger.error(f"[{side}] Exception dans on_need_data: {e}")
                import traceback
                traceback.print_exc()

        # Connecter le signal need-data
        appsrc.connect("need-data", on_need_data)
        
        # Démarrer la pipeline
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"Impossible de démarrer la pipeline {side}")
        
        logger.info(f"Pipeline {side} démarrée avec succès sur port SRT {port}")
        return None

    def _on_bus_message(self, bus, message, side):
        """Traite les messages du bus GStreamer de façon plus détaillée"""
        t = message.type
        
        if t == Gst.MessageType.EOS:
            logger.info(f"Fin du flux {side}, redémarrage...")
            self.pipelines[side].set_state(Gst.State.NULL)
            self.pipelines[side].set_state(Gst.State.PLAYING)
                
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"Erreur GStreamer {side}: {err.message}, {debug}")
            
            # Analyse plus détaillée de l'erreur
            src = message.src
            logger.error(f"Source de l'erreur: {src.get_name()}")
            
            # Tentative de récupération plus robuste
            if self.running:
                logger.info(f"Tentative de récupération du pipeline {side}...")
                self.pipelines[side].set_state(Gst.State.NULL)
                time.sleep(0.5)  # Petit délai avant redémarrage
                self.pipelines[side].set_state(Gst.State.PLAYING)
        
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            logger.warning(f"Avertissement GStreamer {side}: {warn.message}, {debug}")
        
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipelines[side]:
                old, new, pending = message.parse_state_changed()
                logger.debug(f"Pipeline {side} changement d'état: {Gst.Element.state_get_name(old)} -> {Gst.Element.state_get_name(new)}")

    def _klv_sender_thread(self):
        """Thread pour envoyer périodiquement des données KLV en synchronisation avec les images"""
        timestamp = 0
        
        # Décider quelle paire d'images utiliser
        num_pairs = min(len(self.left_images), len(self.right_images))
        
        while self.running and num_pairs > 0:
            for i in range(num_pairs):
                if not self.running:
                    break
                    
                start_time = time.time()
                
                # Préparer les paquets KLV pour gauche et droite
                left_path = self.left_images[i % len(self.left_images)]
                right_path = self.right_images[i % len(self.right_images)]
                
                try:
                    # Création des paquets KLV
                    left_klv = self._create_klv_packet(left_path, timestamp)
                    right_klv = self._create_klv_packet(right_path, timestamp)
                    
                    # Envoi des données KLV via GStreamer
                    self._send_klv_data(left_klv)
                    time.sleep(0.01)  # Petit délai entre les envois
                    self._send_klv_data(right_klv)
                    
                    # Mise à jour du timestamp
                    timestamp += 1
                    
                    # Log
                    if i % 10 == 0:  # Réduire la verbosité des logs
                        logger.info(f"Paire de KLV {i+1}/{num_pairs} envoyée: {os.path.basename(left_path)} et {os.path.basename(right_path)}")
                    
                    # Respect du frame rate
                    processing_time = time.time() - start_time
                    sleep_time = max(0, self.frame_delay - processing_time)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                        
                except Exception as e:
                    logger.error(f"Erreur lors de l'envoi des KLV: {e}")
                    import traceback
                    traceback.print_exc()
                    
            logger.info("Fin d'un cycle d'envoi KLV, redémarrage...")
        
        logger.info("Fin de l'envoi des KLV")
    
    def _run_gst_main_loop(self):
        """Exécute la boucle principale GStreamer"""
        self.main_loop = GLib.MainLoop()
        
        try:
            logger.info("Démarrage de la boucle GStreamer...")
            self.main_loop.run()
        except Exception as e:
            logger.error(f"Erreur dans la boucle GStreamer: {e}")
        finally:
            logger.info("Fin de la boucle GStreamer")
    
    def start(self):
        """Démarre tous les services de streaming"""
        self.running = True
        
        try:
            # Créer les pipelines GStreamer
            temp_dirs = []
            
            # Configurer le pipeline TCP pour KLV
            self._setup_tcp_pipeline()
            
            # Configurer les pipelines SRT pour les flux vidéo
            temp_dirs.append(self._setup_gstreamer_pipeline("left"))
            temp_dirs.append(self._setup_gstreamer_pipeline("right"))
            
            # Démarrer le thread d'envoi KLV
            self.klv_thread = threading.Thread(target=self._klv_sender_thread)
            self.klv_thread.daemon = True
            self.klv_thread.start()
            
            # Démarrer la boucle principale dans un thread séparé
            self.main_loop_thread = threading.Thread(target=self._run_gst_main_loop)
            self.main_loop_thread.daemon = True
            self.main_loop_thread.start()
            
            logger.info("Tous les services sont démarrés")
            logger.info(f"Flux TCP KLV disponible sur TCP {self.tcp_host}:{self.tcp_port}")
            logger.info(f"Flux SRT gauche disponible sur SRT {self.srt_host}:{self.srt_port_left}")
            logger.info(f"Flux SRT droit disponible sur SRT {self.srt_host}:{self.srt_port_right}")
            logger.info("Vous pouvez maintenant démarrer votre SRTStreamProcessor")
            
            # Attendre que l'utilisateur interrompe avec Ctrl+C
            while self.running:
                time.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("Arrêt demandé par l'utilisateur")
        except Exception as e:
            logger.error(f"Erreur au démarrage des services: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.stop()
            # Nettoyer les répertoires temporaires
            for temp_dir in temp_dirs:
                if temp_dir:
                    try:
                        import shutil
                        shutil.rmtree(temp_dir)
                    except:
                        pass
    
    def stop(self):
        """Arrête tous les services"""
        logger.info("Arrêt des services...")
        self.running = False
        
        # Arrêter la boucle principale
        if self.main_loop and self.main_loop.is_running():
            self.main_loop.quit()
        
        # Arrêter les pipelines
        for side, pipeline in self.pipelines.items():
            if pipeline:
                pipeline.set_state(Gst.State.NULL)
                
        logger.info("Tous les services ont été arrêtés")

def main():
    parser = argparse.ArgumentParser(description='Générateur de flux SRT et TCP local pour tests avec GStreamer')
    parser.add_argument('--folder', type=str, default='/home/ivm/test_pipe/imgs', help='Dossier contenant les images')
    parser.add_argument('--srt-host', type=str, default='127.0.0.1', help='Adresse IP pour les flux SRT (localhost)')
    parser.add_argument('--tcp-host', type=str, default='127.0.0.1', help='Adresse IP pour le flux TCP (localhost)')
    parser.add_argument('--srt-port-left', type=int, default=6020, help='Port pour le flux SRT gauche')
    parser.add_argument('--srt-port-right', type=int, default=6040, help='Port pour le flux SRT droit')
    parser.add_argument('--tcp-port', type=int, default=6010, help='Port pour le flux TCP (KLV)')
    parser.add_argument('--frame-rate', type=float, default=5.0, help='Images par seconde (réduit pour le test local)')
    parser.add_argument('--pattern-left', type=str, default='LRL', help='Motif pour identifier les images gauches')
    parser.add_argument('--pattern-right', type=str, default='LRR', help='Motif pour identifier les images droites')
    
    args = parser.parse_args()
    
    try:
        print(f"\n{'='*60}")
        print("Générateur de flux SRT et TCP local avec GStreamer")
        print(f"{'='*60}")
        print(f"Dossier d'images: {args.folder}")
        print(f"Adresse TCP: {args.tcp_host}:{args.tcp_port}")
        print(f"Adresse SRT gauche: {args.srt_host}:{args.srt_port_left}")
        print(f"Adresse SRT droite: {args.srt_host}:{args.srt_port_right}")
        print(f"Frame rate: {args.frame_rate} fps")
        print(f"{'='*60}\n")
        
        generator = GstStreamGenerator(
            image_folder=args.folder,
            tcp_host=args.tcp_host,
            srt_host=args.srt_host,
            srt_port_left=args.srt_port_left,
            srt_port_right=args.srt_port_right,
            tcp_port=args.tcp_port,
            frame_rate=args.frame_rate,
            pattern_left=args.pattern_left,
            pattern_right=args.pattern_right
        )
        
        print("\nDémarrage des serveurs. Ctrl+C pour arrêter.")
        generator.start()
    except KeyboardInterrupt:
        print("\nProgramme interrompu par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur: {e}")
        import traceback
        traceback.print_exc()
        return 1
        
    return 0

if __name__ == "__main__":
    Gst.debug_set_active(True)
    Gst.debug_set_default_threshold(3)  # Niveau INFO
    sys.exit(main())