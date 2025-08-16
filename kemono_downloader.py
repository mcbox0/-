import sys
import os
import re
import time
import threading
import requests
import chromedriver_autoinstaller  # 新增：自动安装ChromeDriver
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
from selenium import webdriver
from selenium.webdriver.chrome.service import Service  # 新增：Service对象
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QTextEdit, QFileDialog, QProgressBar,
                             QGroupBox)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QTextCursor, QIcon


class DownloadThread(QThread):
    # 信号：用于更新日志和进度
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)  # 当前进度, 总数
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)

    def __init__(self, start_url, download_folder):
        super().__init__()
        self.start_url = start_url
        self.download_folder = download_folder
        self._stop_event = threading.Event()
        self.posts_completed = 0
        self.total_posts = 0
        self.lock = threading.Lock()

    def run(self):
        try:
            self.log_signal.emit(f"开始爬取: {self.start_url}")
            self.log_signal.emit(f"文件将保存到: {self.download_folder}")

            # 初始化浏览器
            options = webdriver.ChromeOptions()
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_argument(
                'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')

            # 使用无头模式（不需要图形界面）
            options.add_argument('--headless')

            # 自动安装或更新ChromeDriver
            chromedriver_autoinstaller.install()

            # 创建浏览器实例
            driver = webdriver.Chrome(service=Service(), options=options)
            wait = WebDriverWait(driver, 20)

            try:
                # 访问初始页面
                driver.get(self.start_url)
                self.log_signal.emit(f"已访问初始页面: {self.start_url}")

                # 等待内容加载
                card_list = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".card-list__items"))
                )

                # 获取所有预览链接
                preview_links = []
                cards = card_list.find_elements(By.CSS_SELECTOR,
                                                ".post-card.post-card--preview .fancy-link.fancy-link--kemono")
                for card in cards:
                    href = card.get_attribute('href')
                    if href:
                        preview_links.append(href)

                self.total_posts = len(preview_links)
                self.log_signal.emit(f"找到 {self.total_posts} 个帖子页面")
                self.progress_signal.emit(0, self.total_posts)  # 初始化进度条

                if self.total_posts == 0:
                    self.log_signal.emit("未找到任何帖子，请检查URL是否正确")
                    return

                # 创建图片和视频子目录
                img_dir = os.path.join(self.download_folder, "images")
                vid_dir = os.path.join(self.download_folder, "videos")
                if not os.path.exists(img_dir):
                    os.makedirs(img_dir)
                if not os.path.exists(vid_dir):
                    os.makedirs(vid_dir)

                # 使用线程池处理每个帖子
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = []
                    for idx, link in enumerate(preview_links, 1):
                        if self._stop_event.is_set():
                            self.log_signal.emit("爬取已取消")
                            break

                        self.log_signal.emit(f"提交任务: 帖子 {idx}/{self.total_posts}")
                        futures.append(executor.submit(self.process_post, link, self.download_folder, idx))

                    # 等待所有任务完成
                    for future in futures:
                        if self._stop_event.is_set():
                            # 取消所有未完成的任务
                            for f in futures:
                                f.cancel()
                            break
                        try:
                            future.result()
                        except Exception as e:
                            self.log_signal.emit(f"任务执行出错: {str(e)}")

                self.log_signal.emit("\n所有任务已完成")

            except Exception as e:
                self.error_signal.emit(f"主流程出错: {str(e)}")
            finally:
                driver.quit()
                self.log_signal.emit("浏览器已关闭")

        except Exception as e:
            self.error_signal.emit(f"初始化出错: {str(e)}")
        finally:
            self.finished_signal.emit()

    def stop(self):
        self._stop_event.set()
        self.log_signal.emit("正在停止爬取任务...")

    def download_file(self, url, folder, referer):
        """下载文件并保存到指定文件夹，包含重试机制"""
        if self._stop_event.is_set():
            return False

        max_retries = 3
        retry_delay = 5  # 每次失败后等待的秒数

        if not url or not url.startswith('http'):
            self.log_signal.emit(f"无效的URL，跳过: {url}")
            return False

        # 确保保存目录存在
        if not os.path.exists(folder):
            os.makedirs(folder)

        # 从URL中提取文件名
        filename = re.sub(r'[^\w\.-]', '_', url.split('/')[-1].split('?')[0])
        filepath = os.path.join(folder, filename)

        # 如果文件已存在，则跳过下载
        if os.path.exists(filepath):
            self.log_signal.emit(f"文件已存在，跳过下载: {filename}")
            return True

        # 下载逻辑，包含重试机制
        for attempt in range(max_retries + 1):
            try:
                if self._stop_event.is_set():
                    return False

                headers = {
                    'Referer': referer,
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }

                response = requests.get(url, headers=headers, stream=True, timeout=30)
                response.raise_for_status()  # 检查HTTP响应状态码是否为2xx

                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                self.log_signal.emit(f"已下载: {filename}")
                return True

            except (requests.exceptions.RequestException, Exception) as e:
                if attempt < max_retries:
                    self.log_signal.emit(f"下载失败 (尝试 {attempt + 1}/{max_retries}): {url} - {str(e)}")
                    time.sleep(retry_delay)
                    continue
                else:
                    self.log_signal.emit(f"达到最大重试次数，下载失败: {url}")
                    return False

    def process_post(self, link, download_folder, post_index):
        """处理单个帖子页面，下载所有相关内容"""
        try:
            self.log_signal.emit(f"开始处理帖子 {post_index}/{self.total_posts}: {link}")

            # 创建一个新的浏览器实例（无头模式）
            options = webdriver.ChromeOptions()
            options.add_argument('--headless')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_argument(
                'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')

            # 自动安装或更新ChromeDriver
            chromedriver_autoinstaller.install()

            thread_driver = webdriver.Chrome(service=Service(), options=options)
            thread_wait = WebDriverWait(thread_driver, 20)

            # 访问帖子页面
            thread_driver.get(link)

            # 等待页面主要内容加载
            thread_wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".post__body"))
            )

            # 获取当前页面URL作为Referer
            current_url = thread_driver.current_url

            # 下载MP4附件
            try:
                attachments_section = thread_driver.find_element(By.CSS_SELECTOR, ".post__attachments")
                attachments = attachments_section.find_elements(By.CSS_SELECTOR, ".post__attachment-link")

                for attachment in attachments:
                    if self._stop_event.is_set():
                        break

                    mp4_url = attachment.get_attribute('href')
                    if mp4_url and ('.mp4' in mp4_url.lower() or '.mov' in mp4_url.lower()):
                        self.log_signal.emit(f"找到MP4链接: {mp4_url}")

                        # 视频保存在"videos"子目录
                        save_folder = os.path.join(download_folder, "videos")
                        self.download_file(mp4_url, save_folder, current_url)

                        time.sleep(1)  # 避免请求过快
            except NoSuchElementException:
                self.log_signal.emit(f"未找到MP4附件区域")
            except Exception as e:
                self.log_signal.emit(f"处理MP4附件时出错: {str(e)}")

            # 下载图片附件
            try:
                files_section = thread_driver.find_element(By.CSS_SELECTOR, ".post__files")
                thumbnails = files_section.find_elements(By.CSS_SELECTOR, ".post__thumbnail")

                for thumb in thumbnails:
                    if self._stop_event.is_set():
                        break

                    try:
                        img_link = thumb.find_element(By.CSS_SELECTOR, ".fileThumb.image-link")
                        img_url = img_link.get_attribute('href')
                        if img_url:
                            # 检查常见图片扩展名
                            img_exts = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
                            if any(ext in img_url.lower() for ext in img_exts):
                                self.log_signal.emit(f"找到图片链接: {img_url}")

                                # 图片保存在"images"子目录
                                save_folder = os.path.join(download_folder, "images")
                                self.download_file(img_url, save_folder, current_url)

                                time.sleep(0.5)  # 避免请求过快
                    except NoSuchElementException:
                        continue
            except NoSuchElementException:
                self.log_signal.emit(f"未找到图片附件区域")
            except Exception as e:
                self.log_signal.emit(f"处理图片附件时出错: {str(e)}")

            # 关闭线程专用的浏览器实例
            thread_driver.quit()

            # 更新进度 - 帖子处理完成
            with self.lock:
                self.posts_completed += 1
                self.progress_signal.emit(self.posts_completed, self.total_posts)

            self.log_signal.emit(f"帖子处理完成: {link} ({self.posts_completed}/{self.total_posts})")
        except TimeoutException:
            self.log_signal.emit(f"页面加载超时: {link}")
        except Exception as e:
            self.log_signal.emit(f"处理页面 {link} 时出错: {str(e)}")
        finally:
            # 确保即使出错也更新进度
            with self.lock:
                self.posts_completed += 1
                self.progress_signal.emit(self.posts_completed, self.total_posts)


class KemonoDownloaderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(" ")
        self.setGeometry(100, 100, 800, 600)

        # 设置应用图标
        self.setWindowIcon(QIcon(self.create_icon()))

        # 创建主部件和布局
        main_widget = QWidget()
        main_layout = QVBoxLayout()

        # 创建URL输入区域
        url_group = QGroupBox("爬取设置")
        url_layout = QVBoxLayout()

        # 网址输入
        url_input_layout = QHBoxLayout()
        url_label = QLabel("目标URL:")
        self.url_input = QLineEdit("")
        url_input_layout.addWidget(url_label)
        url_input_layout.addWidget(self.url_input)
        url_layout.addLayout(url_input_layout)

        # 保存路径选择
        path_layout = QHBoxLayout()
        path_label = QLabel("保存路径:")
        self.path_input = QLineEdit(os.path.expanduser("~/Downloads/Kemono"))
        browse_button = QPushButton("浏览...")
        browse_button.clicked.connect(self.browse_directory)
        path_layout.addWidget(path_label)
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(browse_button)
        url_layout.addLayout(path_layout)

        url_group.setLayout(url_layout)
        main_layout.addWidget(url_group)

        # 创建控制按钮
        button_layout = QHBoxLayout()
        self.start_button = QPushButton("开始爬取")
        self.start_button.clicked.connect(self.start_crawling)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop_crawling)
        self.stop_button.setEnabled(False)
        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.stop_button)
        main_layout.addLayout(button_layout)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setAlignment(Qt.AlignCenter)
        self.progress_bar.setFormat("等待开始...")
        main_layout.addWidget(self.progress_bar)

        # 日志显示区域
        log_group = QGroupBox("操作日志")
        log_layout = QVBoxLayout()
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        log_layout.addWidget(self.log_output)
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group)

        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

        # 初始化下载线程
        self.download_thread = None

        # 状态变量
        self.is_crawling = False

    def create_icon(self):
        # 创建一个简单的程序图标
        return QIcon.fromTheme("folder-download")

    def browse_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择保存目录", self.path_input.text())
        if directory:
            self.path_input.setText(directory)

    def start_crawling(self):
        if self.is_crawling:
            return

        url = self.url_input.text().strip()
        save_path = self.path_input.text().strip()

        if not url:
            self.log_output.append("错误: 请输入要爬取的URL")
            return

        if not save_path:
            self.log_output.append("错误: 请选择保存路径")
            return

        # 创建保存目录
        try:
            os.makedirs(save_path, exist_ok=True)
        except Exception as e:
            self.log_output.append(f"创建目录失败: {str(e)}")
            return

        # 重置UI状态
        self.log_output.clear()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("初始化爬取任务...")
        self.is_crawling = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        # 创建并启动下载线程
        self.download_thread = DownloadThread(url, save_path)
        self.download_thread.log_signal.connect(self.update_log)
        self.download_thread.progress_signal.connect(self.update_progress)
        self.download_thread.finished_signal.connect(self.crawling_finished)
        self.download_thread.error_signal.connect(self.handle_error)
        self.download_thread.start()

    def stop_crawling(self):
        if self.is_crawling and self.download_thread:
            self.download_thread.stop()
            self.stop_button.setEnabled(False)
            self.log_output.append("正在停止爬取，请稍候...")
            self.progress_bar.setFormat("正在停止爬取任务...")

    def update_log(self, message):
        self.log_output.append(message)
        self.log_output.moveCursor(QTextCursor.End)

    def update_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"处理帖子: {current}/{total}")

    def crawling_finished(self):
        self.is_crawling = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.log_output.append("爬取任务已完成")
        self.progress_bar.setFormat("爬取任务已完成")

    def handle_error(self, error_message):
        self.log_output.append(f"错误: {error_message}")
        self.crawling_finished()

    def closeEvent(self, event):
        if self.is_crawling and self.download_thread:
            self.download_thread.stop()
            if not self.download_thread.wait(3000):  # 等待最多3秒
                self.log_output.append("强制终止爬取线程...")
        event.accept()


if __name__ == "__main__":
    # 确保在Windows系统上正确显示GUI
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KemonoDownloader")

    app = QApplication(sys.argv)

    # 设置应用样式
    app.setStyle("Fusion")

    window = KemonoDownloaderApp()
    window.show()
    sys.exit(app.exec_())