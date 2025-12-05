module.exports = {
  apps: [
    {
      name: 'cafe_bot',
      script: '/home/ubuntu/adele/venv/bin/python',
      args: '/home/ubuntu/adele/main.py',
      cwd: '/home/ubuntu/adele',
      interpreter: 'none',
      env: {
        PYTHONUNBUFFERED: '1'
      },
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      error_file: '/home/ubuntu/.pm2/logs/cafe-bot-error.log',
      out_file: '/home/ubuntu/.pm2/logs/cafe-bot-out.log',
      time: true
    },
    {
      name: 'admin_bot',
      script: '/home/ubuntu/adele/venv/bin/python',
      args: '/home/ubuntu/adele/admin_bot.py',
      cwd: '/home/ubuntu/adele',
      interpreter: 'none',
      env: {
        PYTHONUNBUFFERED: '1'
      },
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      error_file: '/home/ubuntu/.pm2/logs/admin-bot-error.log',
      out_file: '/home/ubuntu/.pm2/logs/admin-bot-out.log',
      time: true
    }
  ]
};
