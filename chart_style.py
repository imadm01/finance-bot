"""
chart_style.py — one shared "look" for every chart the bot draws.
Change the palette or fonts here and every chart updates automatically.
"""

import matplotlib.pyplot as plt

# A muted, elegant palette instead of matplotlib's default primary colors
PALETTE = ["#6C8EBF", "#82B366", "#D6B656", "#B85450", "#9673A6",
           "#4E9A9A", "#D79B00", "#6C6C6C", "#C19ED6"]


def apply_style():
    """Sets global matplotlib defaults. Called once before drawing any chart."""
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.size": 11,
        "axes.edgecolor": "#DDDDDD",
        "axes.labelcolor": "#333333",
        "axes.grid": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "text.color": "#333333",
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "xtick.color": "#666666",
        "ytick.color": "#666666",
    })


def new_figure(figsize=(6, 5)):
    """Returns (fig, ax) with the style already applied and a clean high-res canvas."""
    apply_style()
    fig, ax = plt.subplots(figsize=figsize, dpi=150)
    return fig, ax
