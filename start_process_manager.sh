#!/bin/bash

# Load conda into shell
source ~/miniconda3/etc/profile.d/conda.sh

# Activate your environment
conda activate process-manager

# Go to your server directory
cd /home/rishabh/server-files

# Run the server
python server.py
