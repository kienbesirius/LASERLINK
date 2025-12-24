# src.link_main.py
from src import *
# from src.core import *
from src.gui.gui_H_code_trigger import LASERLINKAPP as LASERLINK_LASER_SEND_H_CODE_TRIGGER
from src.gui.gui_Laser_NeedPSN_trigger import LASERLINKAPP as LASERLINK_LASER_SEND_NEEDPSN_TRIGGER
def link_main():
    app = LASERLINK_LASER_SEND_NEEDPSN_TRIGGER()
    
    if ("--mock" in sys.argv) or (os.environ.get("LASERLINK_MOCK_UI", "").strip() == "1"):
        app.init_mock_ui(True)
    app.mainloop()