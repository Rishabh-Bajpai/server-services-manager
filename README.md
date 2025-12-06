# Server Services Manager

A robust, web-based process manager for controlling server services and terminals.

![App Screenshot](resources/readme_screenshot.png)

## Features

- **Service Management**: Start, stop, restart, and monitor services.
- **Web Terminal**: Integrated multi-tab terminal for direct server control.
- **Real-time Updates**: Live status updates and logs via WebSockets.
- **Responsive UI**: Modern, dark-themed interface built with Tailwind CSS.

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/Rishabh-Bajpai/server-services-manager.git
   cd server-services-manager
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

## Usage

1. Start the server:

   ```bash
   python server.py
   ```

   Or use the startup script:

   ```bash
   ./start_process_manager.sh
   ```

2. Open your browser and navigate to `http://localhost:8001` (or the configured port).

## Configuration

- **Services**: Define your services via the GUI as shown below:
  
  ![Configuration Screenshot](resources/Config_screenshot.png)

  Alternatively, define your services manually in `config.yaml`.

  Example `config.yaml`:

  ```yaml
  programs:
    - autostart: true
      command: conda run -n comfyui --no-capture-output python main.py --listen
      cwd: /home/rishabh/ComfyUI
      environment: {}
      name: ComfyUI
    - autostart: false
      command: localsend
      cwd: /home/rishabh/Downloads
      environment: {}
      name: localsend
  ```

- **Environment**: Use `.env` for environment variables (e.g., `PORT`, `SECRET_KEY`).

## License

MIT
