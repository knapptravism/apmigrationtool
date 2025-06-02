# Aruba AOS8 to AOS10 AP Migration Tool – Setup Guide

This guide explains how to set up and run the Aruba AOS8 to AOS10 AP Migration Tool on both **Ubuntu Linux** and **macOS** systems.

---

## 🛠️ Prerequisites

Before using this script, ensure the following:
- You are using a machine with Python 3.8+ installed.
- The Aruba Controllers you are targeting:
  - Can reach the internet
  - Have DNS configured
  - Have Activate enabled
  - ONLY FOR CONTROLLERS IN CLUSTERS, NO STANDALONE
  - MCR Required

---

## 🔧 Installation – Ubuntu 22.04 / 24.04

1. **Update packages and install Python3:**
   ```bash
   sudo apt update && sudo apt install -y python3 python3-pip
   ```

2. **(Optional but recommended) Create a virtual environment:**
   ```bash
   python3 -m venv aos_env
   source aos_env/bin/activate
   ```

3. **Install required Python packages using `requirements.txt`:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Make the script executable (if needed):**
   ```bash
   chmod +x aos8_aos10_tool.py
   ```

5. **Run the tool:**
   ```bash
   python3 aos8_aos10_tool.py
   ```

---

## 🍎 Installation – macOS (Ventura or later)

1. **Install Homebrew (if not already installed):**
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

2. **Install Python3:**
   brew install python


3. **Create a virtual environment (optional):**

   python3 -m venv aos_env
   source aos_env/bin/activate

4. **Install Python packages using `requirements.txt`:**

   pip3 install -r requirements.txt


5. **Run the tool:**
   Navigate to the script directory and execute:
   python3 aos8_aos10_tool.py


---

## 🚀 Usage Instructions

After launching the script, you will be presented with a menu-driven interface that allows you to:

- Retrieve Aruba controller and AP data
- Confirm AP groups
- Perform controlled migrations AP GROUPS

> ⚠️ **Warning**: Always ensure your Aruba controllers meet the prerequisites (internet connectivity, DNS, and Activate enabled) before using this tool.

---

## 📦 Included Files

- `aos8_aos10_tool.py`: The main Python script
- `requirements.txt`: Dependency list for easy installation

---

