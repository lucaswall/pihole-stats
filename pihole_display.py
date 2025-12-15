#!/usr/bin/env python3
"""
Pi-hole + Unbound Stats Display

A real-time status display for Raspberry Pi running Pi-hole and Unbound DNS,
using a 3.5" TFT SPI LCD screen (ILI9488 controller).

Hardware: Raspberry Pi 3/4/5 + 3.5" TFT SPI LCD (480x320)
Software: Pi-hole v6+, Unbound DNS resolver

Author: Lucas Wall
License: MIT
"""

import spidev
import RPi.GPIO as GPIO
import time
import subprocess
import json
import urllib.request
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
import os

# =============================================================================
# DISPLAY CONFIGURATION
# =============================================================================

# Hardware dimensions (portrait mode - native orientation)
HW_WIDTH = 320
HW_HEIGHT = 480

# Logical dimensions (landscape mode - how we draw)
WIDTH = 480
HEIGHT = 320

# GPIO Pin assignments for SPI display
DC_PIN = 24   # Data/Command pin
RST_PIN = 25  # Reset pin

# =============================================================================
# PI-HOLE CONFIGURATION
# =============================================================================

# Password loaded from environment variable (set in .env file)
PIHOLE_PASSWORD = os.environ.get('PIHOLE_PASSWORD', '')
PIHOLE_API = "http://localhost/api"  # Pi-hole API endpoint

if not PIHOLE_PASSWORD:
    print("WARNING: PIHOLE_PASSWORD environment variable not set!")

# Session management
pihole_sid = None      # Cached session ID
sid_timestamp = 0      # When the session was created
SID_LIFETIME = 1700    # Session validity in seconds (Pi-hole sessions expire at 1800s)

# =============================================================================
# CPU USAGE TRACKING
# =============================================================================

# We need to track CPU usage between calls to calculate delta
last_cpu_idle = 0
last_cpu_total = 0

# =============================================================================
# GPIO AND SPI INITIALIZATION
# =============================================================================

# Set up GPIO using BCM pin numbering
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(DC_PIN, GPIO.OUT)
GPIO.setup(RST_PIN, GPIO.OUT)

# Initialize SPI bus
spi = spidev.SpiDev()
spi.open(0, 0)  # Bus 0, Device 0
spi.max_speed_hz = 16000000  # 16 MHz SPI clock
spi.mode = 0  # SPI mode 0 (CPOL=0, CPHA=0)

# =============================================================================
# LOW-LEVEL DISPLAY FUNCTIONS
# =============================================================================

def send_command(cmd):
    """
    Send a command byte to the display.
    DC pin LOW indicates command mode.
    """
    GPIO.output(DC_PIN, GPIO.LOW)
    spi.xfer2([cmd])


def send_data(data):
    """
    Send data to the display.
    DC pin HIGH indicates data mode.
    Handles single bytes, lists, and large bytearrays (chunked for SPI buffer limits).
    """
    GPIO.output(DC_PIN, GPIO.HIGH)
    if isinstance(data, int):
        spi.xfer2([data])
    elif isinstance(data, list):
        spi.xfer2(data)
    else:
        # Large data - send in 4KB chunks to avoid SPI buffer overflow
        for i in range(0, len(data), 4096):
            spi.xfer2(list(data[i:i+4096]))


def reset():
    """
    Hardware reset the display.
    Toggle RST pin LOW then HIGH with appropriate delays.
    """
    GPIO.output(RST_PIN, GPIO.HIGH)
    time.sleep(0.1)
    GPIO.output(RST_PIN, GPIO.LOW)
    time.sleep(0.1)
    GPIO.output(RST_PIN, GPIO.HIGH)
    time.sleep(0.2)


def init_display():
    """
    Initialize the ILI9488 display controller.
    
    This sends the complete initialization sequence including:
    - Gamma correction settings
    - Power control
    - Memory access control (orientation)
    - Pixel format (18-bit color)
    - Display inversion settings
    """
    reset()
    
    # Positive gamma correction
    send_command(0xE0)
    send_data([0x00,0x03,0x09,0x08,0x16,0x0A,0x3F,0x78,0x4C,0x09,0x0A,0x08,0x16,0x1A,0x0F])
    
    # Negative gamma correction
    send_command(0xE1)
    send_data([0x00,0x16,0x19,0x03,0x0F,0x05,0x32,0x45,0x46,0x04,0x0E,0x0D,0x35,0x37,0x0F])
    
    # Power control 1
    send_command(0xC0)
    send_data([0x17, 0x15])
    
    # Power control 2
    send_command(0xC1)
    send_data([0x41])
    
    # VCOM control
    send_command(0xC5)
    send_data([0x00, 0x12, 0x80])
    
    # Memory access control - sets display orientation
    # 0x80 = portrait mode with vertical flip
    send_command(0x36)
    send_data(0x80)
    
    # Pixel format - 18-bit color (262K colors)
    # Required for ILI9488, 16-bit causes display issues
    send_command(0x3A)
    send_data(0x66)
    
    # Interface mode control
    send_command(0xB0)
    send_data(0x00)
    
    # Frame rate control
    send_command(0xB1)
    send_data([0xA0])
    
    # Display inversion control
    send_command(0xB4)
    send_data(0x02)
    
    # Display function control
    send_command(0xB6)
    send_data([0x02, 0x02])
    
    # Set image function
    send_command(0xE9)
    send_data(0x00)
    
    # Adjust control 3
    send_command(0xF7)
    send_data([0xA9, 0x51, 0x2C, 0x82])
    
    # Display inversion OFF (0x20) - required for correct colors on this panel
    send_command(0x20)
    
    # Sleep out
    send_command(0x11)
    time.sleep(0.12)
    
    # Display on
    send_command(0x29)
    time.sleep(0.02)


def set_window(x0, y0, x1, y1):
    """
    Set the drawing window (rectangular area) on the display.
    
    Args:
        x0, y0: Top-left corner coordinates
        x1, y1: Bottom-right corner coordinates
    """
    # Column address set
    send_command(0x2A)
    send_data([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
    
    # Row address set
    send_command(0x2B)
    send_data([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
    
    # Memory write
    send_command(0x2C)


def display_image(img):
    """
    Display a PIL Image on the LCD.
    
    The image is rotated -90 degrees for landscape orientation,
    then converted to 18-bit color format with R/B channels swapped
    (BGR order required by this display).
    
    Args:
        img: PIL Image object (480x320 landscape)
    """
    # Rotate for landscape display (software rotation since hardware is portrait)
    img_rotated = img.rotate(-90, expand=True)
    
    # Convert to 18-bit color (3 bytes per pixel)
    # Display expects BGR order, so we swap R and B channels
    pixels = bytearray(HW_WIDTH * HW_HEIGHT * 3)
    idx = 0
    for pixel in img_rotated.getdata():
        pixels[idx] = pixel[2] & 0xFC     # Blue (from R position)
        pixels[idx+1] = pixel[1] & 0xFC   # Green
        pixels[idx+2] = pixel[0] & 0xFC   # Red (from B position)
        idx += 3
    
    # Send pixel data to display
    set_window(0, 0, HW_WIDTH - 1, HW_HEIGHT - 1)
    send_data(pixels)


# =============================================================================
# PI-HOLE API FUNCTIONS
# =============================================================================

def get_pihole_sid():
    """
    Get a Pi-hole API session ID.
    
    Caches the session to avoid rate limiting (429 errors).
    Sessions are valid for 1800 seconds; we refresh at 1700s.
    
    Returns:
        str: Session ID, or None if authentication failed
    """
    global pihole_sid, sid_timestamp
    
    # Reuse existing session if still valid
    if pihole_sid and (time.time() - sid_timestamp) < SID_LIFETIME:
        return pihole_sid
    
    try:
        # Authenticate with Pi-hole API
        data = json.dumps({"password": PIHOLE_PASSWORD}).encode('utf-8')
        req = urllib.request.Request(
            f"{PIHOLE_API}/auth",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            pihole_sid = result.get("session", {}).get("sid")
            sid_timestamp = time.time()
            print(f"New Pi-hole session obtained")
            return pihole_sid
    except Exception as e:
        print(f"Auth error: {e}")
        pihole_sid = None
        return None


def get_pihole_stats(sid):
    """
    Fetch Pi-hole statistics from the API.
    
    Args:
        sid: Valid session ID from get_pihole_sid()
    
    Returns:
        dict: Statistics including queries, clients, gravity info
        None: If request failed
    """
    if not sid:
        return None
    try:
        req = urllib.request.Request(
            f"{PIHOLE_API}/stats/summary",
            headers={"X-FTL-SID": sid}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Session expired, clear it to force re-authentication
            global pihole_sid
            pihole_sid = None
        print(f"Stats error: {e}")
        return None
    except Exception as e:
        print(f"Stats error: {e}")
        return None


# =============================================================================
# UNBOUND DNS FUNCTIONS
# =============================================================================

def get_unbound_stats():
    """
    Fetch Unbound DNS resolver statistics.
    
    Uses unbound-control to get stats without resetting counters.
    
    Returns:
        dict: Key-value pairs of Unbound statistics
        Empty dict: If command failed
    """
    try:
        result = subprocess.run(
            ["sudo", "unbound-control", "stats_noreset"],
            capture_output=True, text=True, timeout=5
        )
        stats = {}
        for line in result.stdout.strip().split('\n'):
            if '=' in line:
                key, value = line.split('=', 1)
                stats[key] = value
        return stats
    except Exception as e:
        print(f"Unbound error: {e}")
        return {}


# =============================================================================
# SYSTEM MONITORING FUNCTIONS
# =============================================================================

def get_cpu_usage():
    """
    Calculate CPU usage percentage since last call.
    
    Reads /proc/stat and calculates the difference in idle vs total time.
    Must be called periodically (e.g., every 5 seconds) for accurate readings.
    
    Returns:
        float: CPU usage percentage (0-100)
    """
    global last_cpu_idle, last_cpu_total
    
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
            fields = line.split()[1:]
            idle = int(fields[3])
            total = sum(int(x) for x in fields[:8])
        
        if last_cpu_total == 0:
            # First run - store values and return 0
            last_cpu_idle = idle
            last_cpu_total = total
            return 0.0
        
        # Calculate delta since last reading
        idle_delta = idle - last_cpu_idle
        total_delta = total - last_cpu_total
        
        # Update stored values for next call
        last_cpu_idle = idle
        last_cpu_total = total
        
        if total_delta > 0:
            return 100.0 * (1.0 - idle_delta / total_delta)
        return 0.0
    except:
        return 0.0


def get_system_stats():
    """
    Collect various system statistics.
    
    Returns:
        dict: System stats including:
            - cpu_temp: CPU temperature in Celsius
            - cpu_usage: CPU usage percentage
            - ram_usage: RAM usage percentage
            - disk_usage: Root partition usage percentage
            - uptime: System uptime in seconds
            - ip: Primary IP address
            - hostname: System hostname
    """
    stats = {}
    
    # CPU Temperature - read from thermal zone
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            stats['cpu_temp'] = int(f.read().strip()) / 1000  # Convert millidegrees to degrees
    except:
        stats['cpu_temp'] = None
    
    # CPU Usage - use the persistent tracking function
    stats['cpu_usage'] = get_cpu_usage()
    
    # RAM Usage - parse /proc/meminfo
    try:
        with open('/proc/meminfo', 'r') as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                meminfo[parts[0].rstrip(':')] = int(parts[1])
            
            total = meminfo['MemTotal']
            available = meminfo.get('MemAvailable', meminfo['MemFree'])
            stats['ram_usage'] = 100 * (1 - available / total)
            stats['ram_total'] = total / 1024 / 1024  # Convert KB to GB
            stats['ram_used'] = (total - available) / 1024 / 1024
    except:
        stats['ram_usage'] = None
    
    # Disk Usage - use statvfs for root partition
    try:
        st = os.statvfs('/')
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        stats['disk_usage'] = 100 * used / total
        stats['disk_total'] = total / 1024 / 1024 / 1024  # Convert to GB
        stats['disk_used'] = used / 1024 / 1024 / 1024
    except:
        stats['disk_usage'] = None
    
    # System Uptime - read from /proc/uptime
    try:
        with open('/proc/uptime', 'r') as f:
            stats['uptime'] = float(f.read().split()[0])
    except:
        stats['uptime'] = None
    
    # IP Address - get from hostname command
    try:
        result = subprocess.run(
            ['hostname', '-I'],
            capture_output=True, text=True, timeout=2
        )
        stats['ip'] = result.stdout.strip().split()[0]
    except:
        stats['ip'] = None
    
    # Hostname - read from /etc/hostname
    try:
        with open('/etc/hostname', 'r') as f:
            stats['hostname'] = f.read().strip()
    except:
        stats['hostname'] = None
    
    return stats


# =============================================================================
# FORMATTING HELPER FUNCTIONS
# =============================================================================

def format_number(n):
    """
    Format large numbers with K/M suffix.
    
    Args:
        n: Number to format
    
    Returns:
        str: Formatted string (e.g., "1.5M", "250K", "42")
    """
    if n >= 1000000:
        return f"{n/1000000:.1f}M"
    elif n >= 1000:
        return f"{n/1000:.1f}K"
    return str(n)


def format_uptime(seconds):
    """
    Format uptime in human-readable format.
    
    Args:
        seconds: Uptime in seconds
    
    Returns:
        str: Formatted string (e.g., "5d 3h 20m", "2h 15m", "45m")
    """
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    mins = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    elif hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def get_status_color(value, warning_threshold, critical_threshold, invert=False):
    """
    Determine status color based on thresholds.
    
    Args:
        value: The metric value to evaluate
        warning_threshold: Value at which to show yellow
        critical_threshold: Value at which to show red
        invert: If True, lower values are worse (e.g., cache hit ratio)
    
    Returns:
        tuple: RGB color tuple (GREEN, YELLOW, or RED)
    """
    GREEN = (0, 255, 0)
    YELLOW = (255, 255, 0)
    RED = (255, 0, 0)
    
    if value is None:
        return RED
    
    if invert:
        # Lower is worse (e.g., cache hit ratio)
        if value <= critical_threshold:
            return RED
        elif value <= warning_threshold:
            return YELLOW
        return GREEN
    else:
        # Higher is worse (e.g., temperature, usage)
        if value >= critical_threshold:
            return RED
        elif value >= warning_threshold:
            return YELLOW
        return GREEN


# =============================================================================
# MAIN DISPLAY DRAWING FUNCTION
# =============================================================================

def draw_stats():
    """
    Create the stats display image.
    
    Fetches all statistics and draws them on a PIL Image with:
    - Header with title, hostname, IP, and clock
    - Pi-hole statistics (left column)
    - Unbound statistics (right column)
    - System stats (CPU, RAM, Disk, Uptime)
    - Status indicators at bottom
    
    Returns:
        PIL.Image: The complete display image (480x320)
    """
    # Fetch all statistics
    sid = get_pihole_sid()
    pihole = get_pihole_stats(sid)
    unbound = get_unbound_stats()
    system = get_system_stats()
    
    # Create blank landscape image
    img = Image.new('RGB', (WIDTH, HEIGHT), 'black')
    draw = ImageDraw.Draw(img)
    
    # Load fonts (with fallback to default)
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except:
        font_large = ImageFont.load_default()
        font_medium = font_large
        font_small = font_large
    
    # Define colors
    GREEN = (0, 255, 0)
    RED = (255, 0, 0)
    CYAN = (0, 255, 255)
    YELLOW = (255, 255, 0)
    WHITE = (255, 255, 255)
    GRAY = (128, 128, 128)
    ORANGE = (255, 165, 0)
    
    # =========================================================================
    # HEADER SECTION
    # =========================================================================
    
    # Title
    draw.text((10, 5), "Pi-hole", font=font_large, fill=GREEN)
    draw.text((110, 5), "+ Unbound", font=font_large, fill=CYAN)
    
    # Hostname and IP (top right area)
    if system.get('hostname') and system.get('ip'):
        draw.text((270, 5), f"{system['hostname']}", font=font_small, fill=GRAY)
        draw.text((270, 20), f"{system['ip']}", font=font_small, fill=GRAY)
    
    # Current time (top right corner)
    now = datetime.now().strftime("%H:%M:%S")
    draw.text((395, 5), now, font=font_medium, fill=WHITE)
    
    # Header divider line
    draw.line([(10, 35), (470, 35)], fill=GRAY, width=1)
    
    # =========================================================================
    # PI-HOLE STATISTICS (LEFT COLUMN)
    # =========================================================================
    
    y_offset = 40
    if pihole and "queries" in pihole:
        q = pihole["queries"]
        g = pihole["gravity"]
        c = pihole["clients"]
        
        # Total queries
        draw.text((10, y_offset), "Queries:", font=font_medium, fill=WHITE)
        draw.text((100, y_offset), format_number(q["total"]), font=font_medium, fill=GREEN)
        
        # Blocked queries with percentage
        y_offset += 25
        draw.text((10, y_offset), "Blocked:", font=font_medium, fill=WHITE)
        blocked_pct = q['percent_blocked']
        # Color based on block percentage (high blocking might indicate issues)
        blocked_color = GREEN if blocked_pct < 30 else (YELLOW if blocked_pct < 50 else ORANGE)
        draw.text((100, y_offset), format_number(q["blocked"]), font=font_medium, fill=blocked_color)
        draw.text((160, y_offset), f"({blocked_pct:.1f}%)", font=font_small, fill=blocked_color)
        
        # Cached queries
        y_offset += 25
        draw.text((10, y_offset), "Cached:", font=font_medium, fill=WHITE)
        draw.text((100, y_offset), format_number(q["cached"]), font=font_medium, fill=CYAN)
        
        # Forwarded queries
        y_offset += 25
        draw.text((10, y_offset), "Forward:", font=font_medium, fill=WHITE)
        draw.text((100, y_offset), format_number(q["forwarded"]), font=font_medium, fill=ORANGE)
        
        # Active/total clients
        y_offset += 25
        draw.text((10, y_offset), "Clients:", font=font_medium, fill=WHITE)
        draw.text((100, y_offset), f"{c['active']}/{c['total']}", font=font_medium, fill=WHITE)
        
        # Blocklist size
        y_offset += 25
        draw.text((10, y_offset), "Blocklist:", font=font_medium, fill=WHITE)
        draw.text((100, y_offset), format_number(g["domains_being_blocked"]), font=font_medium, fill=RED)
        
        pihole_ok = True
    else:
        draw.text((10, y_offset), "Pi-hole: Error", font=font_medium, fill=RED)
        pihole_ok = False
    
    # =========================================================================
    # COLUMN DIVIDER
    # =========================================================================
    
    draw.line([(235, 40), (235, 195)], fill=GRAY, width=1)
    
    # =========================================================================
    # UNBOUND STATISTICS (RIGHT COLUMN)
    # =========================================================================
    
    y_offset = 40
    draw.text((245, y_offset), "Unbound DNS", font=font_medium, fill=CYAN)
    
    if unbound:
        # Parse Unbound stats
        total_queries = int(float(unbound.get("total.num.queries", 0)))
        cache_hits = int(float(unbound.get("total.num.cachehits", 0)))
        uptime = float(unbound.get("time.up", 0))
        avg_recursion = float(unbound.get("total.recursion.time.avg", 0)) * 1000  # Convert to ms
        
        # Calculate cache hit ratio
        cache_ratio = (cache_hits / total_queries * 100) if total_queries > 0 else 0
        
        # Total queries
        y_offset += 25
        draw.text((245, y_offset), "Queries:", font=font_medium, fill=WHITE)
        draw.text((345, y_offset), format_number(total_queries), font=font_medium, fill=GREEN)
        
        # Cache hit ratio (higher is better)
        y_offset += 25
        draw.text((245, y_offset), "Cache hit:", font=font_medium, fill=WHITE)
        cache_color = get_status_color(cache_ratio, 50, 20, invert=True)
        draw.text((345, y_offset), f"{cache_ratio:.1f}%", font=font_medium, fill=cache_color)
        
        # Average recursion time (lower is better)
        y_offset += 25
        draw.text((245, y_offset), "Avg time:", font=font_medium, fill=WHITE)
        time_color = get_status_color(avg_recursion, 100, 500)
        draw.text((345, y_offset), f"{avg_recursion:.1f}ms", font=font_medium, fill=time_color)
        
        # Unbound uptime
        y_offset += 25
        draw.text((245, y_offset), "Uptime:", font=font_medium, fill=WHITE)
        draw.text((345, y_offset), format_uptime(uptime), font=font_medium, fill=WHITE)
        
        unbound_ok = True
    else:
        y_offset += 25
        draw.text((245, y_offset), "Connection error", font=font_medium, fill=RED)
        unbound_ok = False
    
    # =========================================================================
    # SYSTEM STATISTICS SECTION
    # =========================================================================
    
    # Section divider
    draw.line([(10, 200), (470, 200)], fill=GRAY, width=1)
    draw.text((10, 205), "System", font=font_medium, fill=WHITE)
    
    # CPU Temperature
    cpu_temp = system.get('cpu_temp')
    temp_color = get_status_color(cpu_temp, 60, 75) if cpu_temp else RED
    temp_str = f"{cpu_temp:.0f}°C" if cpu_temp else "N/A"
    draw.text((10, 230), "CPU:", font=font_small, fill=WHITE)
    draw.text((45, 230), temp_str, font=font_small, fill=temp_color)
    
    # CPU Usage
    cpu_usage = system.get('cpu_usage')
    cpu_color = get_status_color(cpu_usage, 70, 90)
    cpu_str = f"{cpu_usage:.0f}%"
    draw.text((100, 230), cpu_str, font=font_small, fill=cpu_color)
    
    # RAM Usage
    ram_usage = system.get('ram_usage')
    ram_color = get_status_color(ram_usage, 70, 85) if ram_usage else RED
    ram_str = f"{ram_usage:.0f}%" if ram_usage else "N/A"
    draw.text((150, 230), "RAM:", font=font_small, fill=WHITE)
    draw.text((190, 230), ram_str, font=font_small, fill=ram_color)
    
    # Disk Usage
    disk_usage = system.get('disk_usage')
    disk_color = get_status_color(disk_usage, 70, 90) if disk_usage else RED
    disk_str = f"{disk_usage:.0f}%" if disk_usage else "N/A"
    draw.text((240, 230), "Disk:", font=font_small, fill=WHITE)
    draw.text((285, 230), disk_str, font=font_small, fill=disk_color)
    
    # System Uptime
    sys_uptime = system.get('uptime')
    uptime_str = format_uptime(sys_uptime) if sys_uptime else "N/A"
    draw.text((340, 230), "Up:", font=font_small, fill=WHITE)
    draw.text((370, 230), uptime_str, font=font_small, fill=GREEN)
    
    # =========================================================================
    # STATUS BAR (BOTTOM)
    # =========================================================================
    
    # Status bar divider
    draw.line([(10, 255), (470, 255)], fill=GRAY, width=1)
    
    # Pi-hole status indicator
    pihole_status_color = GREEN if pihole_ok else RED
    draw.rectangle([(10, 265), (25, 280)], fill=pihole_status_color)
    draw.text((30, 263), "Pi-hole", font=font_small, fill=pihole_status_color)
    
    # Unbound status indicator
    unbound_status_color = GREEN if unbound_ok else RED
    draw.rectangle([(110, 265), (125, 280)], fill=unbound_status_color)
    draw.text((130, 263), "Unbound", font=font_small, fill=unbound_status_color)
    
    # System health indicator (aggregate of CPU temp, RAM, and disk)
    sys_ok = True
    sys_warn = False
    
    # Check for critical conditions
    if cpu_temp and cpu_temp >= 75:
        sys_ok = False
    elif cpu_temp and cpu_temp >= 60:
        sys_warn = True
    if ram_usage and ram_usage >= 85:
        sys_ok = False
    elif ram_usage and ram_usage >= 70:
        sys_warn = True
    if disk_usage and disk_usage >= 90:
        sys_ok = False
    elif disk_usage and disk_usage >= 70:
        sys_warn = True
    
    # Determine system status color
    if not sys_ok:
        sys_status_color = RED
    elif sys_warn:
        sys_status_color = YELLOW
    else:
        sys_status_color = GREEN
    
    draw.rectangle([(220, 265), (235, 280)], fill=sys_status_color)
    draw.text((240, 263), "System", font=font_small, fill=sys_status_color)
    
    # =========================================================================
    # BORDER
    # =========================================================================
    
    draw.rectangle([2, 2, WIDTH-3, HEIGHT-3], outline=GRAY, width=1)
    
    return img


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    print("Initializing display...")
    init_display()
    
    print("Starting stats display (Ctrl+C to exit)...")
    
    try:
        while True:
            # Draw and display stats
            img = draw_stats()
            display_image(img)
            
            # Update every 5 seconds
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        # Clear display on exit
        img = Image.new('RGB', (WIDTH, HEIGHT), 'black')
        display_image(img)
