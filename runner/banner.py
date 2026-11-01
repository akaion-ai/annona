"""
Akaion Banner and ASCII Art

Brand colors and visual elements for terminal output.
"""

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from runner import __version__

console = Console()

# Akaion brand colors
AKAION_GRADIENT = ["#5CBBAE", "#3E9E93", "#2E8279", "#1F6F68"]
ANNONA_ACCENT = "#5CBBAE"
AKAION_CYAN = "#5CBBAE"
AKAION_BLUE = "#3E9E93"
AKAION_DARK = "#1F6F68"


def print_banner():
    """Print the wordmark, in the project's colours."""
    banner = r"""
   ___    _   _   _  _____  _   _    ___
  / _ \  | \ | | | ||  _  || \ | |  / _ \
 / /_\ \ |  \| | | || | | ||  \| | / /_\ \
 |  _  | | . ` | | || | | || . ` | |  _  |
 | | | | | |\  | | || |_| || |\  | | | | |
 \_| |_/ \_| \_/ |_| \___/ \_| \_/ \_| |_/
    """

    # Create gradient text
    lines = banner.split("\n")
    text = Text()

    for i, line in enumerate(lines):
        # Calculate color index based on line position
        color_idx = min(i * len(AKAION_GRADIENT) // len(lines), len(AKAION_GRADIENT) - 1)
        text.append(line + "\n", style=f"bold {AKAION_GRADIENT[color_idx]}")

    console.print(text, end="")


def print_runner_banner():
    """Print the full runner startup banner"""
    banner = r"""
   ___    _   _   _  _____  _   _    ___
  / _ \  | \ | | | ||  _  || \ | |  / _ \
 / /_\ \ |  \| | | || | | ||  \| | / /_\ \
 |  _  | | . ` | | || | | || . ` | |  _  |
 | | | | | |\  | | || |_| || |\  | | | | |
 \_| |_/ \_| \_/ |_| \___/ \_| \_/ \_| |_/
    """

    # Create gradient banner
    lines = banner.split("\n")
    gradient_text = Text()

    for i, line in enumerate(lines):
        if line.strip():  # Skip empty lines
            # Gradient from cyan to blue
            color_idx = min(i * len(AKAION_GRADIENT) // len(lines), len(AKAION_GRADIENT) - 1)
            gradient_text.append(line + "\n", style=f"bold {AKAION_GRADIENT[color_idx]}")

    # Subtitle
    from runner import branding

    subtitle = Text()
    subtitle.append("🛡️  ", style="bold")
    subtitle.append(f"{branding.CATEGORY} ", style=f"bold {ANNONA_ACCENT}")
    subtitle.append(f"v{__version__}", style="dim")
    subtitle.append("  ·  ", style="dim white")
    subtitle.append(branding.TAGLINE, style=f"italic {AKAION_BLUE}")

    # Print banner
    console.print()
    console.print(gradient_text, end="")
    console.print(subtitle, justify="center")
    console.print()


def print_runner_box():
    """Print a styled box with runner info"""
    text = Text()
    text.append("🚀 ", style="bold yellow")
    text.append("AKAION", style=f"bold {AKAION_CYAN}")
    text.append(" RUNNER\n", style=f"bold {AKAION_BLUE}")
    text.append("Local Agent Execution Platform", style=f"italic dim {AKAION_BLUE}")

    panel = Panel(
        text,
        border_style=AKAION_CYAN,
        padding=(1, 2),
        title=f"[bold cyan]v{__version__}[/bold cyan]",
        subtitle="[dim]🔗 Connected to Cloud[/dim]",
    )

    console.print(panel)


def print_startup_info(runner_id: str, cloud_url: str, polling_interval: int):
    """Print startup information"""
    from rich.table import Table

    table = Table(show_header=False, border_style=AKAION_BLUE, padding=(0, 1))
    table.add_column(style=f"bold {AKAION_CYAN}")
    table.add_column(style="white")

    table.add_row("🆔 Runner ID", runner_id)
    table.add_row("🌐 Cloud", cloud_url)
    table.add_row("⏱️  Poll Interval", f"{polling_interval}s")
    table.add_row("🔐 Auth", "✓ Authenticated")

    console.print(table)
    console.print()


def print_status_header():
    """Print status check header"""
    text = Text()
    text.append("⚡ ", style="bold yellow")
    text.append("AKAION", style=f"bold {AKAION_CYAN}")
    text.append(" Status Check", style=f"bold {AKAION_BLUE}")
    console.print(text)
    console.print()


def print_simple_logo():
    """One line of identity, from the one place that owns it.

    Read from :mod:`runner.branding` rather than typed here, because the point
    of that module is that renaming the project is one edit — and a hardcoded
    name in a banner is exactly how a rename ends up half-done and shipped.
    """
    from runner import branding

    text = Text()
    text.append("🛡️  ", style="bold")
    text.append(branding.NAME.upper(), style=f"bold {ANNONA_ACCENT}")
    text.append(f"  {branding.TAGLINE}", style="dim")
    console.print(text)
