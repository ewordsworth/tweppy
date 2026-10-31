#!/usr/bin/env python3
"""
Production startup script for Koyeb / Render deployment
"""
import os
from webapp import app, resume_workers

if __name__ == "__main__":
    # Get port from environment (the host sets this)
    port = int(os.environ.get("PORT", 5000))

    # Start the scheduler, and resume auto-posting if it was on before the restart
    resume_workers()

    app.run(host='0.0.0.0', port=port, debug=False)
