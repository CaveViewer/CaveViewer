# Multi-stage Dockerfile for building CaveViewer Linux AppImage
# Can be built and run on macOS, Windows, or Linux

# Stage 1: Build the AppImage
FROM python:3.12 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libfreetype6-dev \
    libgl1-mesa-dev \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    python3-tk \
    python3-dev \
    tk-dev \
    tcl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy source code
COPY . .

# Build steps
RUN echo "Building CaveViewer AppImage..." && \
    ./scripts/linux/build_linux_app.sh && \
    ./scripts/linux/package.sh

# Stage 2: Runtime test image (optional, for verification)
FROM ubuntu:24.04 AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6 \
    libglx0 \
    libgl1-mesa-dri \
    python3-tk \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/dist/linux/packages /app

WORKDIR /app

# Make AppImage executable
RUN chmod +x *.AppImage

# Default command: show help
CMD ["sh", "-c", "ls -lh *.AppImage && echo 'AppImage ready for distribution'"]
