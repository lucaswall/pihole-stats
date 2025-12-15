# Pi-hole + Unbound Stats Display

Real-time stats display for Raspberry Pi running Pi-hole and Unbound DNS on a 3.5" TFT LCD.

## Features

- Pi-hole: queries, blocked, cached, clients, blocklist size
- Unbound: queries, cache hit ratio, avg response time, uptime
- System: CPU temp/usage, RAM, disk, uptime
- Color-coded status indicators (green/yellow/red)

## Hardware

- Raspberry Pi 3/4/5
- 3.5" SPI TFT LCD (ILI9488 controller, 480x320)
- GPIO: DC=24, RST=25, SPI0

## Installation

### 1. Enable SPI
```bash
sudo raspi-config nonint do_spi 0
```

### 2. Install dependencies
```bash
sudo apt install python3-pip python3-pil python3-numpy
sudo pip3 install --break-system-packages spidev RPi.GPIO
```

### 3. Install script
```bash
sudo mkdir -p /opt/pihole-display
sudo cp pihole_display.py /opt/pihole-display/
sudo chown -R root:root /opt/pihole-display
sudo chmod 744 /opt/pihole-display/pihole_display.py
```

### 4. Configure Pi-hole app password
```bash
HASH=$(echo -n "displaystats" | sha256sum | cut -d' ' -f1)
sudo pihole-FTL --config webserver.api.app_pwhash "$HASH"
sudo pihole-FTL --config webserver.api.max_sessions 64
sudo systemctl restart pihole-FTL
```

### 5. Create environment file
```bash
echo "PIHOLE_PASSWORD=displaystats" | sudo tee /opt/pihole-display/.env
sudo chmod 600 /opt/pihole-display/.env
```

### 6. Create service
```bash
sudo tee /etc/systemd/system/pihole-display.service << 'EOF'
[Unit]
Description=Pi-hole Stats Display
After=network.target pihole-FTL.service

[Service]
ExecStart=/usr/bin/python3 /opt/pihole-display/pihole_display.py
WorkingDirectory=/opt/pihole-display
EnvironmentFile=/opt/pihole-display/.env
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF
```

### 7. Start service
```bash
sudo systemctl daemon-reload
sudo systemctl enable pihole-display
sudo systemctl start pihole-display
```

## Management
```bash
sudo systemctl status pihole-display   # Status
sudo systemctl restart pihole-display  # Restart
sudo journalctl -u pihole-display -f   # Logs
```
