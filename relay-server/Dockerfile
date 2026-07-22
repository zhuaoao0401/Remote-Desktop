# 远程桌面中继服务器 Docker 镜像
FROM python:3.9-slim

WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 暴露端口
EXPOSE 9090

# 启动中继服务器
CMD ["python", "relay.py", "--host", "0.0.0.0", "--port", "9090"]
