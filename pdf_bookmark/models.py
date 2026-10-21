from dataclasses import dataclass


@dataclass
class TextLine:
    """页面上的一行（或合并后的短文本块），坐标单位 pt，原点在页面左上角。"""

    text: str
    page: int  # 0 基页码
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float = 0.0
    page_height: float = 0.0
    font_size: float = 0.0
    bold: bool = False
    from_ocr: bool = False
    layout_type: str = ""  # PP-Structure 版面类别，空表示未知
    ocr_score: float = 1.0


@dataclass
class LayoutRegion:
    type: str
    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 0.0


@dataclass
class Heading:
    title: str
    page: int  # 0 基页码
    y: float = 0.0
    level: int = 1
    score: float = 0.0
    font_ratio: float = 1.0
    style: str = ""
