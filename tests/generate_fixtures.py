"""生成不含真实页面内容或元数据的测试图片。运行时指定中文字体路径。"""
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', required=True, type=Path, help='本机中文 TrueType/OpenType 字体文件')
    args = parser.parse_args()
    font = ImageFont.truetype(str(args.font), 32)
    samples = {
        'image_quiz/question': '关于函数的描述，以下错误的是（ ）',
        'image_quiz/a': '周期函数一定有最小正周期',
        'image_quiz/b': '任何一个定义在对称区间上的函数可以写成一个奇函数与一个偶函数的和',
        'image_quiz/c': '偶函数与奇函数的乘积为奇函数',
        'formula_quiz/question': 'f(x) = x^2, -2 <= x < 0',
    }
    root = Path(__file__).parent / 'fixtures'
    for name, text in samples.items():
        left, top, right, bottom = font.getbbox(text)
        image = Image.new('RGB', (right - left + 8, bottom - top + 8), 'white')
        ImageDraw.Draw(image).text((4 - left, 4 - top), text, font=font, fill='black')
        path = root / f'{name}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)


if __name__ == '__main__':
    main()
