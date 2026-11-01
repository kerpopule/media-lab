"""Generate an authored public schematic, never copy a private performance."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parents[1] / "static" / "templates" / "duet-music-video-planning.gif"


def build(output=OUT):
    frames = []
    font = ImageFont.load_default(size=20)
    small = ImageFont.load_default(size=15)
    for i in range(16):
        im = Image.new("RGB", (480, 270), "#0f172a")
        d = ImageDraw.Draw(im)
        phase = 0 if i < 8 else 1 if i < 12 else 2 if i < 14 else 3
        labels = ["SHARED HOOK / ANCHOR", "SOLO / SECONDARY SPACE", "DETAIL INSERT / NO SINGING", "RETURN / SHARED HOOK"]
        d.text((20, 18), labels[phase], font=font, fill="#f8fafc")
        d.rounded_rectangle((20, 55, 460, 211), radius=10, fill=["#26354a", "#22453e", "#44334b", "#26354a"][phase])
        if phase == 2:
            d.ellipse((195, 90, 285, 180), fill="#c4b5fd")
            d.text((196, 184), "SCENERY", font=small, fill="#f8fafc")
        else:
            for x, name, color in ((160, "LEAD A", "#7dd3fc"), (320, "LEAD B", "#fca5a5")):
                if phase == 1 and name == "LEAD B":
                    continue
                x += i % 2 * 3
                d.ellipse((x - 18, 85, x + 18, 121), fill=color)
                d.rounded_rectangle((x - 25, 129, x + 25, 178), radius=9, fill=color)
                d.text((x - 31, 185), name, font=small, fill="#f8fafc")
        d.text((20, 230), "PLANNING SCHEMATIC / NOT A MODEL RENDER", font=small, fill="#cbd5e1")
        # Small progress indicator keeps insert frames distinct without fake motion.
        d.line((20, 261, 20 + 440 * (i + 1) // 16, 261), fill="#7dd3fc", width=3)
        frames.append(im)
    output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(output, save_all=True, append_images=frames[1:], duration=500, loop=0)
    return output


if __name__ == "__main__":
    print(build())
