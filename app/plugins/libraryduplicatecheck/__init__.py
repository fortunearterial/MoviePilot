import re
import shutil
import threading
from datetime import datetime, timedelta
import os
from collections import defaultdict
from pathlib import Path
import pytz

from app.core.config import settings
from app.modules.emby import Emby
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class LibraryDuplicateCheck(_PluginBase):
    # 插件名称
    plugin_name = "媒体库重复媒体检测"
    # 插件描述
    plugin_desc = "媒体库重复媒体检查，可选保留规则保留其一。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/libraryduplicate.png"
    # 插件版本
    plugin_version = "1.9"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "libraryduplicatecheck_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _paths = {}
    _path_type = {}
    _path_mediatpye = {}
    _notify = False
    _delete_softlink = False
    _cron = None
    _onlyonce = False
    _path = None
    _retain_type = None
    _rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_APIKEY = settings.EMBY_API_KEY

    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._cron = config.get("cron")
            self._delete_softlink = config.get("delete_softlink")
            self._onlyonce = config.get("onlyonce")
            self._retain_type = config.get("retain_type")
            self._path = config.get("path")
            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

            self._paths = {}
            self._path_type = {}
            self._path_mediatpye = {}

            if config.get("path"):
                for path in str(config.get("path")).split("\n"):
                    path_mediatpye = '电影'
                    if path.count("%") == 1:
                        path_mediatpye = path.split("%")[1]
                        path = path.split("%")[0]

                    retain_type = self._retain_type
                    if path.count("$") == 1:
                        retain_type = path.split("$")[1]
                        path = path.split("$")[0]

                    if path.count("#") == 1:
                        library_name = path.split("#")[1]
                        path = path.split("#")[0]
                        self._paths[path] = library_name
                    else:
                        self._paths[path] = None

                    self._path_type[path] = retain_type
                    self._path_mediatpye[path] = path_mediatpye

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"媒体库重复媒体检测服务启动，立即运行一次")
                    self._scheduler.add_job(self.check_duplicate, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="媒体库重复媒体检测")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.check_duplicate,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="媒体库重复媒体检测")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def check_duplicate(self):
        """
        检查媒体库重复媒体
        """
        if not self._paths and not self._paths.keys():
            logger.warning("媒体库重复媒体检测服务未配置路径")
            return

        msg = ""
        for path in self._paths.keys():
            _retain_type = self._path_type.get(path)
            _path_mediatpye = self._path_mediatpye.get(path)
            logger.info(f"开始检查路径：{path} {_retain_type}")
            duplicate_files, delete_duplicate_files, delete_cloud_files = self.__find_duplicate_videos(path,
                                                                                                       _retain_type,
                                                                                                       _path_mediatpye)
            logger.info(f"路径 {path} 检查完毕")

            library_name = self._paths.get(path)
            if library_name:
                logger.info(f"开始刷新媒体库：{library_name}")
                # 获取emby 媒体库
                librarys = Emby().get_librarys()
                if not librarys:
                    logger.error("获取媒体库失败")
                    return

                if str(self._retain_type) != '仅检查' and delete_duplicate_files > 0 or delete_cloud_files > 0:
                    for library in librarys:
                        if not library:
                            continue
                        if library.name == library_name:
                            logger.info(f"媒体库：{library_name} 刷新完成")
                            self.__refresh_emby_library_by_id(library.id)
                            break
            msg += (f"{path}{'#' + library_name if library_name else ''} 检查完成\n"
                    f"文件保留规则: {_retain_type}\n"
                    f"本地重复文件: {duplicate_files}\n"
                    f"删除本地文件: {delete_duplicate_files}\n"
                    f"删除云盘文件: {delete_cloud_files}\n\n")

        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="媒体库重复媒体检测",
                text=msg,
                link=settings.MP_DOMAIN('#/history')
            )

    def __refresh_emby_library_by_id(self, item_id: str) -> bool:
        """
        通知Emby刷新一个项目的媒体库
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return False
        req_url = "%semby/Items/%s/Refresh?Recursive=true&api_key=%s" % (self._EMBY_HOST, item_id, self._EMBY_APIKEY)
        try:
            res = RequestUtils().post_res(req_url)
            if res:
                return True
            else:
                logger.info(f"刷新媒体库对象 {item_id} 失败，无法连接Emby！")
        except Exception as e:
            logger.error(f"连接Items/Id/Refresh出错：" + str(e))
            return False
        return False

    def __find_duplicate_videos(self, directory, retain_type, path_mediatpye):
        """
        检查目录下视频文件是否有重复
        """
        # Dictionary to hold the list of files for each video name
        video_files = defaultdict(list)

        duplicate_files = 0
        delete_duplicate_files = 0
        delete_cloud_files = 0

        # Traverse the directory and subdirectories
        for root, _, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                # Check the file extension
                if (Path(str(file_path)).exists() or os.path.islink(file_path)) and Path(file).suffix.lower() in [
                    ext.strip() for ext in self._rmt_mediaext.split(",")]:
                    video_name = Path(file).stem.split('-')[0].rstrip()
                    if str(path_mediatpye) == '电视剧':
                        # 使用正则表达式匹配
                        match = re.search(r"S\d+E\d+", Path(file).stem)
                        if match:
                            video_name += f" {match.group(0)}"
                    logger.info(f'Scan file -> {file} -> {video_name}')
                    video_files[video_name].append(file_path)

        logger.info("\n================== RESULT ==================\n")

        # 全程加锁
        with lock:
            # 按照 paths 的长度对 video_files 进行排序
            sorted_video_files = sorted(video_files.items(), key=lambda item: len(item[1]))

            # Find and handle duplicate video files
            for name, paths in sorted_video_files:
                if len(paths) > 1:
                    duplicate_files += len(paths)
                    logger.info(f"Duplicate video files for '{name}':")
                    for path in paths:
                        if Path(path).exists() or os.path.islink(path):
                            logger.info(f"  {path} 文件大小：{os.path.getsize(path)}，创建时间：{os.path.getmtime(path)}")
                        else:
                            logger.info(f"  {path} 文件已被删除")

                    # Decide which file to keep based on criteria (e.g., file size or creation date)
                    logger.info(f"文件保留规则：{str(retain_type)}")
                    keep_file = self.__choose_file_to_keep(paths, retain_type)
                    logger.info(f"本地保留文件: {keep_file}")
                    if self._delete_softlink:
                        keep_cloud_file = os.readlink(str(keep_file))
                        logger.info(f"云盘保留文件: {keep_cloud_file}")

                    # Delete the other duplicate files (if needed)
                    for path in paths:
                        if (Path(path).exists() or os.path.islink(path)) and str(path) != str(keep_file):
                            delete_duplicate_files += 1
                            self.__delete_duplicate_file(duplicate_file=path,
                                                         paths=paths,
                                                         keep_file=str(keep_file),
                                                         file_type="监控")
                            if self._delete_softlink:
                                # 同步删除软连接源目录
                                cloud_file = os.readlink(path)
                                if cloud_file and Path(cloud_file).exists():
                                    delete_cloud_files += 1
                                    self.__delete_duplicate_file(duplicate_file=cloud_file,
                                                                 paths=paths,
                                                                 keep_file=keep_cloud_file,
                                                                 file_type="云盘")
                else:
                    logger.info(f"'{name}' No Duplicate video files.")

            return duplicate_files, delete_duplicate_files, delete_cloud_files

    def __delete_duplicate_file(self, duplicate_file, paths, keep_file, file_type):
        """
        删除重复文件
        """
        cloud_file_path = Path(duplicate_file)
        # 删除文件、nfo、jpg等同名文件
        pattern = cloud_file_path.stem.replace('[', '?').replace(']', '?')
        files = list(cloud_file_path.parent.glob(f"{pattern}.*"))
        logger.info(f"筛选 {cloud_file_path.parent} 下同名文件 {pattern}.* {len(files)}个")
        media_files = []
        for file in files:
            if Path(file).suffix.lower() in [ext.strip() for ext in
                                             self._rmt_mediaext.split(",")]:
                media_files.append(Path(file).stem)

        media_files = list(set(media_files))
        if len(media_files) == len(paths):
            # 说明两个重名的同名，删除非keep媒体文件，保留刮削文件
            for file in media_files:
                if str(file) != str(keep_file):
                    if str(self._retain_type) != "仅检查":
                        Path(file).unlink()
                        logger.info(f"{file_type}文件 {file} 已删除")
                    else:
                        logger.warning(f"{file_type}文件 {file} 将被删除")
        else:
            for file in files:
                if str(file) != str(keep_file):
                    if str(self._retain_type) != "仅检查":
                        Path(file).unlink()
                        logger.info(f"{file_type}文件 {file} 已删除")
                    else:
                        logger.warning(f"{file_type}文件 {file} 将被删除")

            # 删除thumb图片
            thumb_file = cloud_file_path.parent / (cloud_file_path.stem + "-thumb.jpg")
            if thumb_file.exists():
                if str(self._retain_type) != "仅检查":
                    thumb_file.unlink()
                    logger.info(f"{file_type}文件 {thumb_file} 已删除")
                else:
                    logger.warning(f"{file_type}文件 {thumb_file} 将被删除")

            self.__rmtree(Path(duplicate_file), file_type)

    def __rmtree(self, path: Path, file_type: str):
        """
        删除目录及其子目录
        """
        # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
        if not SystemUtils.exits_files(path.parent, [ext.strip() for ext in
                                                     self._rmt_mediaext.split(",")]):
            # 判断父目录是否为空, 为空则删除
            for parent_path in path.parents:
                if str(parent_path.parent) != str(path.root):
                    # 父目录非根目录，才删除父目录
                    if not SystemUtils.exits_files(parent_path, [ext.strip() for ext in
                                                                 self._rmt_mediaext.split(",")]):
                        if parent_path.exists():
                            # 当前路径下没有媒体文件则删除
                            if str(self._retain_type) != "仅检查":
                                shutil.rmtree(parent_path)
                                logger.warn(f"{file_type}目录 {parent_path} 已删除")
                            else:
                                logger.warning(f"{file_type}目录 {parent_path} 将被删除")

    @staticmethod
    def __choose_file_to_keep(paths, retain_type):
        checked = None
        checked_path = None

        for path in paths:
            if str(retain_type) == "保留体积最小":
                selected = os.path.getsize(path)
                if checked is None or selected < checked:
                    checked = selected
                    checked_path = path
            elif str(retain_type) == "保留体积最大":
                selected = os.path.getsize(path)
                if checked is None or selected > checked:
                    checked = selected
                    checked_path = path
            elif str(retain_type) == "保留创建最早":
                selected = os.path.getmtime(path)
                if checked is None or selected < checked:
                    checked = selected
                    checked_path = path
            elif str(retain_type) == "保留创建最晚":
                selected = os.path.getmtime(path)
                if checked is None or selected > checked:
                    checked = selected
                    checked_path = path

        return checked_path

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "delete_softlink": self._delete_softlink,
            "notify": self._notify,
            "path": self._path,
            "retain_type": self._retain_type,
            "rmt_mediaext": self._rmt_mediaext
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/libraryduplicatecheck",
            "event": EventType.PluginAction,
            "desc": "媒体库重复媒体检测",
            "category": "",
            "data": {
                "action": "libraryduplicatecheck"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'delete_softlink',
                                            'label': '删除软连接源文件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'retain_type',
                                            'label': '保留规则',
                                            'items': [
                                                {'title': '仅检查', 'value': '仅检查'},
                                                {'title': '保留体积最小', 'value': '保留体积最小'},
                                                {'title': '保留体积最大', 'value': '保留体积最大'},
                                                {'title': '保留创建最早', 'value': '保留创建最早'},
                                                {'title': '保留创建最晚', 'value': '保留创建最晚'},
                                            ]
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path',
                                            'label': '检查路径',
                                            'rows': 2,
                                            'placeholder': "检查的媒体路径\n"
                                                           "检查的媒体路径$保留规则\n"
                                                           "检查的媒体路径#媒体库名称\n"
                                                           "检查的媒体路径#媒体库名称$保留规则\n"
                                                           "检查的媒体路径#媒体库名称$保留规则%电视剧\n"

                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rmt_mediaext',
                                            'label': '视频格式',
                                            'rows': 2,
                                            'placeholder': ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '检测指定路径下同一媒体文件是否有重复（不同扩展名视为同一媒体）。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '检查路径配置`#媒体库`名称时会通知Emby刷新媒体库。检查路径配置`%电视剧`可指定处理媒体的格式。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '检查路径自定义配置`$保留规则`且插件保留规则为`仅检查`时，将会预览操作而不删除重复文件。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "delete_softlink": False,
            "cron": "5 1 * * *",
            "path": "",
            "notify": False,
            "retain_type": "仅检查",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
