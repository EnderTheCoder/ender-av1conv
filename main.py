# app.py
from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import json
import subprocess
import threading
import time
import magic
import uuid
from datetime import datetime
from collections import deque
import shutil

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'

# 配置文件路径
CONFIG_FILE = 'config.json'

# 全局变量
tasks = {}  # 存储所有任务
task_queue = deque()  # 任务队列
active_tasks = {}  # 正在执行的任务
max_concurrent = 4  # 默认最大并发数
scan_directory = "/"  # 扫描目录
ffmpeg_params = "-c:v libaom-av1 -crf 28 -b:v 0 -preset 6 -c:a copy"  # 默认FFmpeg参数
task_lock = threading.Lock()
scan_lock = threading.Lock()

# 支持的视频文件扩展名
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp'}


# 加载配置
def load_config():
    global max_concurrent, scan_directory, ffmpeg_params
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                max_concurrent = config.get('max_concurrent', max_concurrent)
                scan_directory = config.get('scan_directory', scan_directory)
                ffmpeg_params = config.get('ffmpeg_params', ffmpeg_params)
        except Exception as e:
            print(f"加载配置文件失败: {e}")


# 保存配置
def save_config():
    config = {
        'max_concurrent': max_concurrent,
        'scan_directory': scan_directory,
        'ffmpeg_params': ffmpeg_params
    }
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存配置文件失败: {e}")


class VideoConverter:
    def __init__(self, task_id, input_file, output_file, ffmpeg_params):
        self.task_id = task_id
        self.input_file = input_file
        self.output_file = output_file
        self.ffmpeg_params = ffmpeg_params
        self.process = None
        self.start_time = None

    def is_video_file(self, file_path):
        """检查文件是否为视频文件"""
        try:
            mime = magic.Magic(mime=True)
            file_mime = mime.from_file(file_path)
            return file_mime.startswith('video/')
        except:
            # 如果magic库不可用，使用扩展名检查
            return any(file_path.lower().endswith(ext) for ext in VIDEO_EXTENSIONS)

    def get_video_duration(self, file_path):
        """获取视频时长"""
        try:
            cmd = ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                   '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except:
            return 0

    def is_av1_encoded(self, file_path):
        """检查视频是否已经是AV1编码"""
        try:
            cmd = ['ffprobe', '-v', 'quiet', '-show_entries', 'stream=codec_name',
                   '-select_streams', 'v:0', '-of', 'csv=p=0', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            codec = result.stdout.strip()
            return codec.lower() == 'av1'
        except:
            return False

    def run(self):
        """执行转码任务"""
        global tasks, active_tasks
        output_dir = os.path.dirname(self.output_file)
        # 使用临时输出文件名（.开头）
        temp_output_file = os.path.join(output_dir, 'tmp.' + os.path.basename(self.output_file))

        with task_lock:
            tasks[self.task_id]['status'] = 'running'
            tasks[self.task_id]['start_time'] = datetime.now().isoformat()
            active_tasks[self.task_id] = self

        try:
            # 构建FFmpeg命令
            cmd = ['ffmpeg', '-i', self.input_file] + self.ffmpeg_params.split() + [temp_output_file, '-y']

            # 获取视频时长用于进度计算
            duration = self.get_video_duration(self.input_file)

            self.start_time = time.time()
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            # 实时读取FFmpeg输出以获取进度
            while True:
                line = self.process.stderr.readline()
                if line == '' and self.process.poll() is not None:
                    break
                if line:
                    # 解析进度信息
                    if 'time=' in line:
                        try:
                            # 提取时间信息计算进度
                            time_pos = line.find('time=')
                            if time_pos > -1:
                                time_str = line[time_pos + 5:time_pos + 17].strip()
                                if ':' in time_str and '.' in time_str:
                                    parts = time_str.split(':')
                                    if len(parts) == 3:
                                        hours, minutes, seconds = parts
                                        total_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                                        if duration > 0:
                                            progress = min(100, int((total_seconds / duration) * 100))
                                            with task_lock:
                                                tasks[self.task_id]['progress'] = progress
                        except:
                            pass

            # 等待进程结束
            return_code = self.process.wait()

            with task_lock:
                if return_code == 0 and os.path.exists(temp_output_file):
                    # 转码成功，重命名临时文件为最终文件
                    os.rename(temp_output_file, self.output_file)
                    # 删除原文件
                    try:
                        os.remove(self.input_file)
                        tasks[self.task_id]['status'] = 'completed'
                        tasks[self.task_id]['progress'] = 100
                        tasks[self.task_id]['end_time'] = datetime.now().isoformat()
                        tasks[self.task_id]['result'] = 'success'
                    except Exception as e:
                        tasks[self.task_id]['status'] = 'failed'
                        tasks[self.task_id]['error'] = f'文件替换失败: {str(e)}'
                else:
                    # 转码失败，删除临时文件
                    if os.path.exists(temp_output_file):
                        os.remove(temp_output_file)
                    tasks[self.task_id]['status'] = 'failed'
                    tasks[self.task_id]['error'] = '转码失败'

        except Exception as e:
            with task_lock:
                tasks[self.task_id]['status'] = 'failed'
                tasks[self.task_id]['error'] = str(e)
            # 清理临时文件
            if os.path.exists(temp_output_file):
                os.remove(temp_output_file)
        finally:
            with task_lock:
                if self.task_id in active_tasks:
                    del active_tasks[self.task_id]


def find_video_files(directory):
    """扫描目录下的所有视频文件"""
    video_files = []
    try:
        for root, dirs, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                if os.path.isfile(file_path):
                    # 检查扩展名
                    if any(file.lower().endswith(ext) for ext in VIDEO_EXTENSIONS):
                        video_files.append(file_path)
    except Exception as e:
        print(f"扫描目录时出错: {e}")
    return video_files


def task_worker():
    """任务执行工作线程"""
    global task_queue, active_tasks, max_concurrent

    while True:
        time.sleep(1)

        # 检查是否可以启动新任务
        with task_lock:
            if len(active_tasks) >= max_concurrent or len(task_queue) == 0:
                continue

            # 启动新任务
            task_id = task_queue.popleft()
            if task_id in tasks:
                task = tasks[task_id]
                converter = VideoConverter(
                    task_id,
                    task['input_file'],
                    task['output_file'],
                    task['ffmpeg_params']
                )
                thread = threading.Thread(target=converter.run)
                thread.daemon = True
                thread.start()


# 启动任务工作线程
worker_thread = threading.Thread(target=task_worker)
worker_thread.daemon = True
worker_thread.start()

# 程序启动时加载配置
load_config()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config', methods=['GET', 'POST'])
def config():
    global max_concurrent, scan_directory, ffmpeg_params

    if request.method == 'POST':
        data = request.json
        if 'max_concurrent' in data:
            max_concurrent = int(data['max_concurrent'])
        if 'scan_directory' in data:
            scan_directory = data['scan_directory']
        if 'ffmpeg_params' in data:
            ffmpeg_params = data['ffmpeg_params']

        # 保存配置
        save_config()

        return jsonify({'status': 'success'})

    return jsonify({
        'max_concurrent': max_concurrent,
        'scan_directory': scan_directory,
        'ffmpeg_params': ffmpeg_params
    })


@app.route('/api/scan', methods=['POST'])
def scan_videos():
    global scan_directory

    data = request.json
    directory = data.get('directory', scan_directory)

    if not os.path.exists(directory):
        return jsonify({'error': '目录不存在'}), 400

    # 异步扫描文件
    def scan_async():
        if not scan_lock.acquire(blocking=False):
            return jsonify({'error': '已有一个扫描正在进行中'}), 400

        try:
            # 自动清理临时文件（非当前处理中的任务相关的）
            temp_files_to_remove = []

            with task_lock:
                # 获取当前正在运行任务的临时文件名
                current_temp_names = set()
                for task in tasks.values():
                    if task['status'] == 'running':
                        output_dir = os.path.dirname(task['output_file'])
                        output_basename = os.path.basename(task['output_file'])
                        temp_name = 'tmp.' + output_basename
                        current_temp_names.add(temp_name)

                # 获取所有已存在的任务输入文件路径，避免重复创建任务
                existing_input_files = {task['input_file'] for task in tasks.values()
                                        if task['status'] in ['pending', 'running']}

            # 遍历所有文件，找出临时文件
            for root, _, files in os.walk(directory):
                for file in files:
                    if file.startswith('tmp.'):
                        file_path = os.path.join(root, file)
                        temp_file_name = file

                        # 如果不在当前运行任务中，则可以删除
                        if temp_file_name not in current_temp_names:
                            temp_files_to_remove.append(file_path)

            for temp_path in temp_files_to_remove:
                try:
                    os.remove(temp_path)
                except Exception as e:
                    print(f"无法删除临时文件 {temp_path}: {e}")

            # 然后继续正常扫描视频文件
            video_files = find_video_files(directory)

            with task_lock:
                for file_path in video_files:
                    # 跳过已存在的任务
                    if file_path in existing_input_files:
                        continue

                    ext = os.path.splitext(file_path)[1].lower()
                    if ext not in VIDEO_EXTENSIONS:
                        continue  # 忽略非支持的视频格式

                    # 检查是否已经是AV1编码
                    converter = VideoConverter('', file_path, '', '')
                    if converter.is_av1_encoded(file_path):
                        continue  # 跳过AV1编码的视频

                    output_file = os.path.splitext(file_path)[0] + '.mkv'

                    task_id = str(uuid.uuid4())
                    tasks[task_id] = {
                        'id': task_id,
                        'input_file': file_path,
                        'output_file': output_file,
                        'ffmpeg_params': ffmpeg_params,
                        'status': 'pending',
                        'progress': 0,
                        'created_time': datetime.now().isoformat(),
                        'start_time': None,
                        'end_time': None,
                        'error': None
                    }
                    task_queue.append(task_id)
                    existing_input_files.add(file_path)  # 添加到已存在列表中
        except Exception as e:
            print(f"扫描时出错: {e}")
        finally:
            scan_lock.release()

    thread = threading.Thread(target=scan_async)
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'scanning started'})


@app.route('/api/tasks')
def get_tasks():
    """获取任务列表（支持分页）"""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    status_filter = request.args.get('status', 'all')

    with task_lock:
        # 过滤任务
        filtered_tasks = []
        for task in tasks.values():
            if status_filter == 'all' or task['status'] == status_filter:
                filtered_tasks.append(task)

        # 分页
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_tasks = filtered_tasks[start_idx:end_idx]

        return jsonify({
            'tasks': paginated_tasks,
            'total': len(filtered_tasks),
            'page': page,
            'per_page': per_page,
            'total_pages': (len(filtered_tasks) + per_page - 1) // per_page
        })


@app.route('/api/tasks/<task_id>')
def get_task(task_id):
    """获取单个任务详情"""
    with task_lock:
        if task_id in tasks:
            return jsonify(tasks[task_id])
        return jsonify({'error': '任务不存在'}), 404


@app.route('/api/tasks/<task_id>/cancel', methods=['POST'])
def cancel_task(task_id):
    """取消任务"""
    with task_lock:
        if task_id in tasks:
            task = tasks[task_id]
            if task['status'] == 'running' and task_id in active_tasks:
                converter = active_tasks[task_id]
                if converter.process:
                    converter.process.terminate()
            task['status'] = 'cancelled'
            return jsonify({'status': 'success'})
        return jsonify({'error': '任务不存在'}), 404


@app.route('/api/file/size', methods=['POST'])
def get_file_size():
    """获取文件大小"""
    try:
        data = request.get_json()
        file_path = data.get('file_path', '')

        if not file_path or not os.path.exists(file_path):
            return jsonify({'size': 0})

        size = os.path.getsize(file_path)
        return jsonify({'size': size})

    except Exception as e:
        return jsonify({'size': 0})


@app.route('/api/stats')
def get_stats():
    """获取统计信息"""
    with task_lock:
        total = len(tasks)
        pending = sum(1 for task in tasks.values() if task['status'] == 'pending')
        running = sum(1 for task in tasks.values() if task['status'] == 'running')
        completed = sum(1 for task in tasks.values() if task['status'] == 'completed')
        failed = sum(1 for task in tasks.values() if task['status'] == 'failed')
        cancelled = sum(1 for task in tasks.values() if task['status'] == 'cancelled')

        return jsonify({
            'total': total,
            'pending': pending,
            'running': running,
            'completed': completed,
            'failed': failed,
            'cancelled': cancelled,
            'active_workers': len(active_tasks),
            'max_concurrent': max_concurrent
        })


@app.route('/api/stop-all', methods=['POST'])
def stop_all():
    """停止所有转换并清理所有临时文件"""
    global tasks, task_queue, active_tasks

    with task_lock:
        # 停止所有运行中的任务
        for task_id, converter in list(active_tasks.items()):
            if converter.process:
                converter.process.terminate()
            if task_id in tasks:
                tasks[task_id]['status'] = 'cancelled'

        # 清空任务队列
        task_queue.clear()
        active_tasks.clear()

        # 清理所有临时文件
        if scan_directory and os.path.exists(scan_directory):
            for root, _, files in os.walk(scan_directory):
                for file in files:
                    if file.startswith('tmp.'):
                        try:
                            os.remove(os.path.join(root, file))
                        except Exception as e:
                            print(f"无法删除临时文件 {file}: {e}")
    try:
        result = subprocess.run(['pkill', '-9', 'ffmpeg'], capture_output=True, text=True)
        if result.returncode == 0:
            print("成功杀死所有ffmpeg进程")
        else:
            print(f"执行失败: {result.stderr}")
    except Exception as e:
        print(f"发生错误: {e}")

    return jsonify({'status': '所有转换已停止，临时文件已清理'})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
