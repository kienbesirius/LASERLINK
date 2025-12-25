# src.utils.utils.py
from PIL import Image

def png_to_ico(src_path_file: str, out_path_file: str):
    img = Image.open(src_path_file)
    sizes = [(16, 16),(24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(out_path_file, sizes=sizes)

png_to_ico(
    src_path_file="/home/te/Documents/LaserLink/src/assets/icons/laser-link-app-icon.png",
    out_path_file="/home/te/Documents/LaserLink/src/assets/icons/laser-link-app-icon.ico",
)