# src.link_main.py
from src import *
# from src.core import *
from src.gui.gui import LASERLINKAPP
def link_main():
    app = LASERLINKAPP()
    
    if ("--mock" in sys.argv) or (os.environ.get("LASERLINK_MOCK_UI", "").strip() == "1"):
        app.init_mock_ui(True)
    app.mainloop()