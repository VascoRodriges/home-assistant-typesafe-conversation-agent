"""Generate original local HA brand assets from vector geometry (requires Pillow)."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def render(scale, dark=False):
    # The SVG in assets/ is the editable design source. Coordinates are in 256px.
    image = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    factor = scale / 256

    def points(values):
        return [(round(x * factor), round(y * factor)) for x, y in values]

    def box(values):
        return tuple(round(x * factor) for x in values)

    draw.rounded_rectangle(
        box((8, 8, 248, 248)),
        radius=round(54 * factor),
        fill="#142539" if dark else "#163451",
    )
    draw.line(
        points([(48, 116), (128, 49), (208, 116)]),
        fill="#57D9D1",
        width=round(14 * factor),
        joint="curve",
    )
    draw.rounded_rectangle(
        box((64, 111, 192, 210)), radius=round(17 * factor), fill="#F5FAFF"
    )
    draw.rounded_rectangle(
        box((86, 126, 170, 185)), radius=round(17 * factor), fill="#163451"
    )
    draw.polygon(points([(145, 176), (157, 195), (161, 177)]), fill="#163451")
    for x, top, bottom in [
        (104, 148, 165),
        (116, 137, 174),
        (128, 143, 169),
        (140, 137, 174),
        (152, 148, 165),
    ]:
        draw.line(
            points([(x, top), (x, bottom)]), fill="#57D9D1", width=round(5 * factor)
        )
    # Simple typed-decision check mark: a speech agent for the home, not a vendor logo.
    draw.ellipse(box((178, 176, 228, 226)), fill="#57D9D1")
    draw.line(
        points([(189, 201), (198, 210), (215, 191)]),
        fill="#163451",
        width=round(5 * factor),
        joint="curve",
    )
    return image


if __name__ == "__main__":
    destination = ROOT / "custom_components" / "typesafe_conversation" / "brand"
    destination.mkdir(parents=True, exist_ok=True)
    for dark in (False, True):
        for size, suffix in ((256, ""), (512, "@2x")):
            icon = render(size, dark)
            prefix = "dark_" if dark else ""
            icon.save(destination / f"{prefix}icon{suffix}.png", optimize=True)
            icon.save(destination / f"{prefix}logo{suffix}.png", optimize=True)
