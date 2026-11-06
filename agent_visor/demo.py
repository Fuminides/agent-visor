"""Offline demo images, generated with the same dependency as the viewer."""
import math
from pathlib import Path
import random
from PIL import Image, ImageDraw, ImageFont
from .storage import data_dir


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def make_demo():
    directory = data_dir() / "demo"
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for kind, title, subtitle in [
        ("line", "Cada iteración, un poco mejor", "Evolución del error durante el entrenamiento"),
        ("bar", "Verlo cambia la conversación", "Comparación de resultados por método · datos de ejemplo"),
    ]:
        image = Image.new("RGB", (1100, 720), "#ffffff")
        draw = ImageDraw.Draw(image)
        draw.text((75, 44), "VISOR / EJEMPLO", fill="#668675", font=font(15, True))
        draw.text((75, 87), title, fill="#203630", font=font(32, True))
        draw.text((75, 140), subtitle, fill="#718074", font=font(18))
        left, top, right, bottom = 115, 230, 1015, 575
        for i in range(6):
            y = top + (bottom - top) * i / 5
            draw.line((left, y, right, y), fill="#e9eee9", width=1)
            draw.text((63, y - 10), f"{1 - i / 5:.1f}", fill="#7d8b80", font=font(16))
        if kind == "line":
            random.seed(7)
            for color, speed, offset in [("#b8c6ba", 2.2, .12), ("#327e65", 4.8, .04)]:
                points = []
                for i in range(101):
                    value = .82 * math.exp(-speed * i / 100) + offset + random.uniform(-.015, .015)
                    points.append((left + i * 9, bottom - value * (bottom - top)))
                draw.line(points, fill=color, width=4, joint="curve")
            for i in range(6):
                x = left + i * 180
                draw.text((x - 10, bottom + 20), str(i * 20), fill="#7d8b80", font=font(16))
            draw.text((470, 634), "Iteraciones", fill="#63796a", font=font(17))
            draw.rounded_rectangle((690, 183, 710, 195), radius=4, fill="#b8c6ba")
            draw.text((720, 178), "Referencia", fill="#63796a", font=font(16))
            draw.rounded_rectangle((865, 183, 885, 195), radius=4, fill="#327e65")
            draw.text((895, 178), "Ajustado", fill="#63796a", font=font(16))
        else:
            names = ["Base", "Método A", "Método B", "Propuesta"]
            values = [.52, .68, .77, .91]
            for i, (name, value) in enumerate(zip(names, values)):
                x = left + 40 + i * 220
                y = bottom - value * (bottom - top)
                color = "#327e65" if i == 3 else ["#d2dfd5", "#afcbb9", "#84b29b"][i]
                draw.rounded_rectangle((x, y, x + 130, bottom), radius=8, fill=color)
                draw.text((x + 41, y - 36), f"{value:.0%}", fill="#203630", font=font(21, True))
                draw.text((x + 65, bottom + 24), name, fill="#63796a", font=font(17), anchor="mt")
        draw.text((75, 677), "Generado localmente. Listo para que tu agente te enseñe el siguiente resultado.", fill="#8b988d", font=font(14))
        path = directory / ("01-evolución.png" if kind == "line" else "02-comparación.png")
        image.save(path)
        paths.append(path)
    return paths
