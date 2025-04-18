import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GObject

import sys

# Initialise GStreamer
Gst.init(None)

def receive_srt_and_display(host, port):
    """
    Reçoit un flux SRT JPEG RTP et l'affiche à l'écran.
    """
    pipeline_str = (
        f'srtclientsrc uri=srt://{host}:{port} ! '
        'application/x-rtp, encoding-name=JPEG,payload=26 ! '
        'rtpjpegdepay ! jpegdec ! videoconvert ! autovideosink sync=false'
    )

    print(f"Connexion au flux SRT sur srt://{host}:{port}")
    print(f"Pipeline GStreamer :\n{pipeline_str}")

    pipeline = Gst.parse_launch(pipeline_str)
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            print("Fin de flux")
            pipeline.set_state(Gst.State.NULL)
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Erreur : {err}, debug : {debug}")
            pipeline.set_state(Gst.State.NULL)
            loop.quit()

    bus.connect("message", on_message)

    pipeline.set_state(Gst.State.PLAYING)
    loop = GObject.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("Arrêt manuel")
        pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    # if len(sys.argv) != 3:
    #     print("Usage: python receive_srt_and_display.py <host> <port>")
    #     sys.exit(1)

    # host = sys.argv[1]
    # port = int(sys.argv[2])
    receive_srt_and_display("127.0.0.1", "6020")
