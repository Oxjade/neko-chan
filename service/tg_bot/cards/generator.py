"""
Neko-Chan Bot — PnL card generator.

pip install pillow

Fonts: this uses DejaVu Sans (bundled with most Linux distros / Pillow's
test suite) and Poppins-Bold for the title. If Poppins isn't installed on
your server, swap FONT_TITLE for a DejaVu path — see the fallback below.

Origin code from /home/carnage/Downloads/pnl_card.py — kept intact, with
additions only: random template rotation, a caption bank that preserves the
cat persona, and import-safe pathing so the bot can call it directly.
"""

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from collections import Counter
import glob
import os
import random

# ---- palette fallbacks ----
MINT = (140, 255, 210)
WHITE = (250, 250, 250)
CARD_BLACK = (20, 18, 22)

# ---- templates + persona ----
# Folder that holds the cat avatar variants (rotate at random per card).
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TEMPLATES = sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.jpg")) + glob.glob(os.path.join(TEMPLATES_DIR, "*.png")))

# Caption banks — keep the cat's voice. "win" is smug, "loss" is dry.
WIN_CAPTIONS = [
    "she flipped your bag before your coffee got cold",
    "the cat smells green candles",
    "neko sold the top, obviously",
    "purr-fect exit. you're welcome",
    "that's the sound of your bag getting fatter",
    "she read the market like a 5am litter box",
    "green is her favorite color, and today it's yours too",
    "the whiskers twitched. the bags grew.",
    "she cashed out clean — no crumbs left for the bears",
    "this chart went up. so did she.",
]
LOSS_CAPTIONS = [
    "even the cat can't save this one",
    "neko tripped. the bag paid for it",
    "that one stung. she's rethinking her life choices",
    "losses happen. the cat is unbothered",
    "she ate the loss so you don't have to",
    "cat's out of the bag — it was a red day",
    "the cat blinked, and so did the chart",
    "red candles, red carpet, same dignity",
    "she'll bounce back. your bag, eventually.",
    "this one's on the house — the cat's treat",
]
# Small footer line — rotates, always in character, never financial advice.
FOOTERS = [
    "not financial advice. definitely feline advice.",
    "the cat is not a licensed financial advisor. she's just better.",
    "past purrs do not guarantee future gains.",
    "no cats were liquidated in the making of this card.",
]


def _detect_bg_color(img: Image.Image, near_black_thresh: int = 40):
    """
    Sample pixels along the image border and return the most common color
    that isn't near-black (the cat's fill/outline). Falls back to mint if
    every border pixel is dark (e.g. a tight crop with no visible bg).
    """
    img = img.convert("RGB")
    w, h = img.size
    step = max(1, min(w, h) // 200)
    samples = []
    for x in range(0, w, step):
        samples.append(img.getpixel((x, 0)))
        samples.append(img.getpixel((x, h - 1)))
    for y in range(0, h, step):
        samples.append(img.getpixel((0, y)))
        samples.append(img.getpixel((w - 1, y)))

    filtered = [c for c in samples if sum(c) > near_black_thresh]
    if not filtered:
        return MINT
    # round colors slightly so near-identical shades bucket together
    rounded = [tuple(v - v % 8 for v in c) for c in filtered]
    most_common = Counter(rounded).most_common(1)[0][0]
    return most_common


def _accent_for_bg(bg_color):
    """
    Derive an accent color from the avatar's background hue, but always
    keep it bright/saturated — the accent is drawn on the dark card body
    (not on the avatar itself), so it must stay light regardless of how
    light or dark the source background was.
    """
    import colorsys
    r, g, b = [v / 255 for v in bg_color]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    r2, g2, b2 = colorsys.hsv_to_rgb(h, min(max(s, 0.55), 0.85), 0.95)
    return tuple(int(c * 255) for c in (r2, g2, b2))

# ---- fonts (edit paths for your server) ----
FONT_TITLE = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

if not os.path.exists(FONT_TITLE):
    FONT_TITLE = FONT_BOLD  # fallback if Poppins isn't installed


def _font(path, size):
    return ImageFont.truetype(path, size)


def _center_text(draw, text, font, y, W, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, y), text, font=font, fill=fill)
    return bbox[3] - bbox[1]  # text height


def generate_pnl_card(
    avatar_path: str,
    pnl_pct: float,
    buy_price: float,
    sell_price: float,
    token: str,
    chain: str,
    out_path: str,
    caption: str = None,
    bot_name: str = "NEKO-CHAN BOT",
    handle: str = "@nekochan_trades",
    timestamp: str = None,
    bg_color: tuple = None,
):
    """
    Render a PnL card and save it to out_path.

    pnl_pct:    e.g. 269.42 or -12.3  (sign/color handled automatically)
    buy_price:  entry price, in USD
    sell_price: exit price, in USD
    token:      e.g. "$NEKO"
    chain:      e.g. "SOLANA", "SUI", "BASE"
    caption:    optional one-liner; auto-picked from pnl sign if omitted
    bg_color:   optional (R,G,B) override. If omitted, it's auto-detected
                from the avatar's own background — so a pink-bg cat gives
                a pink-glow card, an orange-bg cat gives an orange-glow
                card, etc. This is what lets you rotate through your cat
                variants and have the card match each one automatically.
    """
    W, H = 1080, 1350
    is_win = pnl_pct >= 0

    avatar_img = Image.open(avatar_path).convert("RGB")
    detected_bg = bg_color if bg_color is not None else _detect_bg_color(avatar_img)
    glow_color_win = detected_bg
    accent = _accent_for_bg(detected_bg) if is_win else (255, 120, 130)

    if caption is None:
        caption = random.choice(WIN_CAPTIONS if is_win else LOSS_CAPTIONS)
    if timestamp is None:
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")

    img = Image.new("RGB", (W, H), CARD_BLACK)
    draw = ImageDraw.Draw(img)

    # soft glow behind header — matches the avatar's own background on a
    # win, shifts to a red-tinted glow on a loss
    glow = Image.new("RGB", (W, H), CARD_BLACK)
    gdraw = ImageDraw.Draw(glow)
    glow_color = glow_color_win if is_win else (120, 40, 50)
    gdraw.ellipse([W // 2 - 700, -650, W // 2 + 700, 550], fill=glow_color)
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    img = Image.blend(img, glow, 0.55)
    draw = ImageDraw.Draw(img)

    # border
    draw.rounded_rectangle([10, 10, W - 10, H - 10], radius=40, outline=accent, width=4)

    # header
    draw.text((60, 50), bot_name, font=_font(FONT_TITLE, 54), fill=WHITE)
    draw.text((60, 112), f"{handle}  ·  autotrader", font=_font(FONT_REG, 26), fill=(200, 200, 205))

    # chain badge
    badge_font = _font(FONT_BOLD, 28)
    bbox = draw.textbbox((0, 0), chain, font=badge_font)
    bw = bbox[2] - bbox[0]
    bx2, by1, by2 = W - 60, 55, 105
    bx1 = bx2 - bw - 60
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=25, fill=(30, 30, 34), outline=accent, width=2)
    draw.text((bx1 + 30, by1 + 8), chain, font=badge_font, fill=accent)

    # avatar, circular crop
    cat = ImageOps.fit(avatar_img, (560, 560), method=Image.LANCZOS)
    mask = Image.new("L", (560, 560), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 560, 560], fill=255)
    cat_x, cat_y = (W - 560) // 2, 170

    ring = Image.new("RGB", (W, H), CARD_BLACK)
    ImageDraw.Draw(ring).ellipse([cat_x - 25, cat_y - 25, cat_x + 585, cat_y + 585], fill=accent)
    ring = ring.filter(ImageFilter.GaussianBlur(40))
    img = Image.blend(img, ring, 0.25)
    draw = ImageDraw.Draw(img)
    draw.ellipse([cat_x - 8, cat_y - 8, cat_x + 568, cat_y + 568], outline=accent, width=6)
    img.paste(cat, (cat_x, cat_y), mask)

    # PnL headline
    pnl_str = f"{'+' if is_win else ''}{pnl_pct:.2f}%"
    _center_text(draw, pnl_str, _font(FONT_TITLE, 96), 780, W, accent)
    _center_text(draw, caption, _font(FONT_REG, 30), 895, W, (210, 210, 215))

    # stats row
    stats = [
        ("BUY", f"${buy_price:.6f}" if buy_price < 1 else f"${buy_price:,.2f}"),
        ("SELL", f"${sell_price:.6f}" if sell_price < 1 else f"${sell_price:,.2f}"),
        ("TOKEN", token),
    ]
    sx, sy = 60, 970
    sw = (W - 120) // 3
    for i, (label, val) in enumerate(stats):
        x0 = sx + i * sw
        draw.rounded_rectangle([x0, sy, x0 + sw - 20, sy + 140], radius=20,
                                fill=(28, 26, 32), outline=(60, 60, 66), width=2)
        draw.text((x0 + 24, sy + 22), label, font=_font(FONT_BOLD, 22), fill=(150, 150, 156))
        draw.text((x0 + 24, sy + 62), val, font=_font(FONT_MONO, 28), fill=WHITE)

    # footer
    _center_text(draw, random.choice(FOOTERS),
                 _font(FONT_REG, 24), 1175, W, (150, 150, 156))
    draw.text((60, H - 70), timestamp, font=_font(FONT_REG, 22), fill=(120, 120, 126))
    tag = "neko-chan.bot"
    bbox = draw.textbbox((0, 0), tag, font=_font(FONT_BOLD, 24))
    tw = bbox[2] - bbox[0]
    draw.text((W - 60 - tw, H - 72), tag, font=_font(FONT_BOLD, 24), fill=accent)

    img.save(out_path)
    return out_path


def random_avatar() -> str:
    """Pick a random cat template from the bundled templates folder."""
    if not TEMPLATES:
        return "cat_avatar.jpg"
    return random.choice(TEMPLATES)


if __name__ == "__main__":
    # example: card background auto-matches whichever cat variant you pass
    generate_pnl_card(
        avatar_path=random_avatar(),   # random cat variant each run
        pnl_pct=269.42,
        buy_price=0.000412,
        sell_price=0.001521,
        token="$NEKO",
        chain="SOLANA",
        out_path="nekochan_pnl.png",
    )
    # to force a specific background instead of auto-detecting:
    # generate_pnl_card(..., bg_color=(255, 165, 0))
