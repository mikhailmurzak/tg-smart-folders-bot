from telethon import TelegramClient, events, Button, connection
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl import types
from telethon.sessions import StringSession, MemorySession
import logging
import sys
import asyncio
import qrcode
from io import BytesIO
import json
import os
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

load_dotenv()

# Конфигурация
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATA_DIR = os.getenv('DATA_DIR', '/data')
LOGS_DIR = os.path.join(DATA_DIR, 'logs')

# Настройка логирования
os.makedirs(LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(
            os.path.join(LOGS_DIR, 'bot.log'),
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        ),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class UserSession:
    def __init__(self, user_id, bot_instance):
        self.user_id = user_id
        self.client = None
        self.active_folders = {}  # {folder_id: {"channel_id": channel_id, "title": folder_title}}
        self.folder_handlers = {}
        self.is_authorized = False
        self.session_string = None
        self.bot_instance = bot_instance
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10  # Увеличиваем количество попыток
        self.retry_delay = 5  # Начальная задержка в секундах

    async def init_client(self):
        """Инициализация клиента для пользователя"""
        try:
            # Если клиент существует, пробуем закрыть его
            if self.client:
                try:
                    await self.client.disconnect()
                except:
                    pass
                
            # Загружаем данные пользователя
            data = self.bot_instance.load_user_data(self.user_id)
            self.session_string = data.get('session_string')
            self.active_folders = data.get('active_folders', {})
            
            # Создаем новый клиент
            if self.session_string:
                logger.info(f"Восстанавливаем сессию для пользователя {self.user_id}")
                self.client = TelegramClient(
                    StringSession(self.session_string),
                    API_ID,
                    API_HASH,
                    device_model='Desktop',
                    system_version='Windows 10',
                    app_version='1.0',
                    flood_sleep_threshold=60,
                    request_retries=10,
                    connection_retries=10,
                    retry_delay=5,  # Увеличиваем задержку между попытками
                    timeout=60,  # Увеличиваем таймаут
                    auto_reconnect=True
                )
            else:
                logger.info(f"Создаем новую сессию для пользователя {self.user_id}")
                self.client = TelegramClient(
                    StringSession(),
                    API_ID,
                    API_HASH,
                    device_model='Desktop',
                    system_version='Windows 10',
                    app_version='1.0',
                    flood_sleep_threshold=60,
                    request_retries=10,
                    connection_retries=10,
                    retry_delay=2,
                    timeout=30,
                    auto_reconnect=True
                )

            await self.client.connect()
            
            if await self.client.is_user_authorized():
                self.is_authorized = True
                # Сохраняем сессию только если её ещё нет
                if not self.session_string:
                    self.session_string = self.client.session.save()
                    # Сохраняем данные пользователя с session_string
                    self.bot_instance.save_user_data(self.user_id, {
                        'session_string': self.session_string,
                        'active_folders': self.active_folders,
                        'folder_channels': data.get('folder_channels', {})
                    })
                # Восстанавливаем каналы
                await self.restore_channels()
                return True
                
            return False

        except Exception as e:
            logger.error(f"Ошибка при инициализации клиента: {e}", exc_info=True)
            return False

    async def save_session(self):
        """Сохранение сессии и данных о каналах"""
        try:
            self.bot_instance.save_user_data(self.user_id, {
                'session_string': self.session_string,
                'active_folders': self.active_folders,
                'folder_channels': {
                    folder_id: {
                        'channel_id': data['channel_id'],
                        'title': data['title']
                    }
                    for folder_id, data in self.active_folders.items()
                }
            })
        except Exception as e:
            logger.error(f"Ошибка при сохранении данных: {e}", exc_info=True)

    async def restore_channels(self):
        """Восстановление связей с каналами"""
        try:
            # Проверяем соединение перед восстановлением
            if not await self.ensure_connected():
                logger.error("Не удалось установить соединение для восстановления каналов")
                return

            data = self.bot_instance.load_user_data(self.user_id)
            folder_channels = data.get('folder_channels', {})
            
            # Получаем текущие папки
            dialog_filters = await self.client(GetDialogFiltersRequest())
            current_folders = {
                str(f.id): f 
                for f in dialog_filters.filters 
                if hasattr(f, 'id') and hasattr(f, 'title')
            }

            # Восстанавливаем активные папки
            for folder_id, channel_data in folder_channels.items():
                if folder_id in current_folders:
                    folder = current_folders[folder_id]
                    # Проверяем существование канала
                    try:
                        # Пробуем получить канал через PeerChannel
                        try:
                            peer = types.PeerChannel(channel_data['channel_id'])
                            channel = await self.client.get_entity(peer)
                            logger.info(f"Канал {channel.id} получен через PeerChannel")
                        except Exception as e:
                            logger.warning(f"Не удалось получить канал через PeerChannel: {e}")
                            # Пробуем получить через диалоги
                            async for dialog in self.client.iter_dialogs():
                                if dialog.is_channel and dialog.entity.id == channel_data['channel_id']:
                                    channel = dialog.entity
                                    logger.info(f"Канал {channel.id} найден в диалогах")
                                    break
                            else:
                                raise Exception("Канал не найден в диалогах")

                        if channel:
                            self.active_folders[folder_id] = {
                                'channel_id': channel.id,
                                'title': channel_data['title']
                            }
                            # Восстанавливаем ��ересылку
                            await self.bot_instance.setup_message_forwarding(self, folder, channel.id)
                            logger.info(f"Восстановлена папка {folder.title} с каналом {channel.id}")
                        
                    except Exception as e:
                        logger.error(f"Не удалось восстановить канал для папки {folder.title}: {e}")
                        # Сохраняем информацию о канале даже если не удалось его восстановить
                        if folder_id not in self.active_folders:
                            self.active_folders[folder_id] = {
                                'channel_id': channel_data['channel_id'],
                                'title': channel_data['title']
                            }

            # Сохраняем обновленные данные
            self.bot_instance.save_user_data(self.user_id, {
                'session_string': self.session_string,
                'active_folders': self.active_folders,
                'folder_channels': folder_channels
            })

        except Exception as e:
            logger.error(f"Ошибка при восстановлении каналов: {e}", exc_info=True)

    async def ensure_connected(self):
        """Проверка и восстановление соединения"""
        try:
            if not self.client or not self.client.is_connected():
                if self.reconnect_attempts < self.max_reconnect_attempts:
                    self.reconnect_attempts += 1
                    # Экспоненциальное увеличение задержки
                    delay = self.retry_delay * (2 ** (self.reconnect_attempts - 1))
                    logger.info(f"Попытка переподключения {self.reconnect_attempts}/{self.max_reconnect_attempts} через {delay} секунд")
                    
                    await asyncio.sleep(delay)
                    
                    # Пересоздаем клиент если текущий не работает
                    if not self.client or not await self.client.connect():
                        await self.init_client()
                    
                    # Проверяем авторизацию после переподключения
                    if not await self.client.is_user_authorized():
                        self.is_authorized = False
                        logger.warning("Клиент потерял авторизацию после переподключения")
                        return False
                    
                    logger.info("Успешное переподключение")
                    self.reconnect_attempts = 0  # Сбрасываем счетчик при успехе
                    return True
                else:
                    logger.error(f"Превышено максимальное количество попыток переподключения для пользователя {self.user_id}")
                    self.is_authorized = False
                    # Сбрасываем счетчик для следующей серии попыток
                    self.reconnect_attempts = 0
                    return False
            return True
        except Exception as e:
            logger.error(f"Ошибка при проверке соединения: {e}", exc_info=True)
            return False

    async def ensure_authorized(self):
        """Проверка авторизации"""
        try:
            if not self.client or not self.client.is_connected():
                await self.client.connect()
            
            if not await self.client.is_user_authorized():
                self.is_authorized = False
                return False
                
            return True
        except Exception as e:
            logger.error(f"Ошибка при проверке авторизации: {e}", exc_info=True)
            return False

    async def handle_action(self, action):
        """Обработка действий с проверкой авторизации"""
        try:
            if not await self.ensure_authorized():
                logger.warning("Клиент не авторизован, требуется повторная авторизация")
                return None
            return await action()
        except Exception as e:
            logger.error(f"Ошибка при выполнении действя: {e}", exc_info=True)
            return None

class TelegramBot:
    def __init__(self):
        self.bot = None
        self.users = {}
        self.auth_states = {}
        
        # Создаем необходимые директории в DATA_DIR
        os.makedirs(os.path.join(DATA_DIR, 'user_data'), exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, 'logs'), exist_ok=True)

    def load_user_data(self, user_id):
        """Загрузка данных пользователя"""
        try:
            # Загружаем основные данные пользователя
            file_path = os.path.join(DATA_DIR, 'user_data', f'{user_id}.json')
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"Загружены данные пользователя {user_id}")
            else:
                data = {'active_folders': {}}

            # Загружаем данные о каналах из отдельного файла
            channels_path = os.path.join(DATA_DIR, 'user_data', f'{user_id}_channels.json')
            if os.path.exists(channels_path):
                with open(channels_path, 'r', encoding='utf-8') as f:
                    channels_data = json.load(f)
                    data['folder_channels'] = channels_data.get('folder_channels', {})
                    logger.info(f"Загружены данные о каналах пользователя {user_id}")
            else:
                data['folder_channels'] = {}

            return data
        except Exception as e:
            logger.error(f"Ошибка при загрузке данных пользователя {user_id}: {e}")
            return {'active_folders': {}, 'folder_channels': {}}

    def save_user_data(self, user_id, data):
        """Сохранение данных пользователя"""
        try:
            # Сохраняем основные данные пользователя
            user_data = {
                'session_string': data.get('session_string'),
                'active_folders': data.get('active_folders', {})
            }
            
            file_path = os.path.join(DATA_DIR, 'user_data', f'{user_id}.json')
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(user_data, f, ensure_ascii=False, indent=2)
                logger.info(f"Сохранены основные данные пользователя {user_id}")
            
            # Сохраняем данные о каналах отдельно
            channels_data = {
                'folder_channels': data.get('folder_channels', {})
            }
            
            channels_path = os.path.join(DATA_DIR, 'user_data', f'{user_id}_channels.json')
            with open(channels_path, 'w', encoding='utf-8') as f:
                json.dump(channels_data, f, ensure_ascii=False, indent=2)
                logger.info(f"Сохранены данные о каналах пользователя {user_id}")
            
            # Устанавливаем права доступа для файлов
            os.chmod(file_path, 0o666)
            os.chmod(channels_path, 0o666)
        except Exception as e:
            logger.error(f"Ошибка при сохранении данных пользователя {user_id}: {e}")

    async def get_user_session(self, user_id):
        """Получение или создание сессии пользователя"""
        if user_id not in self.users:
            self.users[user_id] = UserSession(user_id, self)
            # Загружаем сохраненные данные
            data = self.load_user_data(user_id)
            self.users[user_id].active_folders = data.get('active_folders', {})
        return self.users[user_id]

    async def setup(self):
        """Инициализация бота"""
        logger.info("Начало инициализации бота...")
        try:
            # Используем сессию в памяти для бота
            self.bot = TelegramClient(MemorySession(), API_ID, API_HASH)
            await self.bot.start(bot_token=BOT_TOKEN)
            self.register_handlers()
            logger.info("Бот успешно инициализирован")
        except Exception as e:
            logger.error(f"Ошибка при инициализации бота: {e}", exc_info=True)
            raise

    async def show_folders(self, event, user_session):
        """Показ списка папок пользователя"""
        try:
            # Получаем список папок
            dialog_filters = await user_session.client(GetDialogFiltersRequest())
            logger.info(f"Получены фильтры диалогов для пользователя {user_session.user_id}")
            logger.info(f"Активные папки: {user_session.active_folders}")
            
            # Создаем кнопки для папок
            buttons = []
            for folder in dialog_filters.filters:
                if hasattr(folder, 'title') and folder.title:
                    # Проверяем статус папки
                    folder_id_str = str(folder.id)
                    is_active = folder_id_str in user_session.active_folders
                    emoji = "✅" if is_active else "⭕️"
                    logger.info(f"Папка {folder.title} (ID: {folder_id_str}) активна: {is_active}")
                    buttons.append([Button.inline(f"{emoji} {folder.title}", f"folder_{folder.id}")])
            
            if buttons:
                await event.respond(
                    "Выберите папки для создания каналов:",
                    buttons=buttons
                )
            else:
                await event.respond("У вас пока нет папок в Telegram.")
                
        except Exception as e:
            logger.error(f"Ошибка при получении списка папок: {e}", exc_info=True)
            await event.respond("Произошла ошибка при получении списка папок.")

    async def create_folder_channel(self, user_session, folder_title):
        """Создание канала для папки"""
        try:
            logger.info(f"Создаем канал для папки {folder_title}")
            result = await user_session.client(CreateChannelRequest(
                title=f"📁 {folder_title}",
                about=f"Агрегатор для папки {folder_title}",
                megagroup=False,
                for_import=False
            ))
            channel = result.chats[0]
            logger.info(f"Канал создан успешно: {channel.id}")
            return channel
        except Exception as e:
            logger.error(f"Ошибка при создании канала: {e}", exc_info=True)
            return None

    async def setup_message_forwarding(self, user_session, folder, channel_id):
        """Настройка пересылки сообщений для папки"""
        logger.info(f"Настройка пересылки для папки {folder.title}")
        
        async def forward_handler(event):
            try:
                # Проверяем соединение перед обработкой сообщения
                if not await user_session.ensure_connected():
                    logger.warning("Не удаось восстановить соединение")
                    return

                # Получаем информацию о сообщении
                chat = await event.get_chat()
                logger.info(f"Получено сообщение из чата: {chat.id}")
                
                # Получаем список каналов из папки
                included_peers = []
                for peer in folder.include_peers:
                    try:
                        entity = await user_session.client.get_entity(peer)
                        included_peers.append(entity.id)
                    except Exception as e:
                        logger.error(f"Ошибка при получении информации о канале: {e}")
                        continue

                if chat.id in included_peers:
                    # Добавляем неболшую задержку
                    await asyncio.sleep(0.5)
                    
                    try:
                        await user_session.client.forward_messages(
                            channel_id,
                            event.message,
                            silent=True
                        )
                        logger.info("Сообщение успешно пееслано")
                    except Exception as e:
                        logger.error(f"Ошибка при пересылке: {e}")
                        # Пробуем переподключиться
                        await user_session.init_client()
                
            except Exception as e:
                logger.error(f"Ошибка при обработке сообщения: {e}", exc_info=True)
        
        # Регистрируем обаботчик
        handler = user_session.client.add_event_handler(
            forward_handler,
            events.NewMessage(chats=None)
        )
        user_session.folder_handlers[folder.id] = handler

    def register_handlers(self):
        @self.bot.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            user_id = event.sender_id
            logger.info(f"Получена команда /start от пользователя {user_id}")
            
            user_session = await self.get_user_session(user_id)
            
            if not await user_session.init_client():
                await self.start_auth_process(event, user_session)
            else:
                await self.show_folders(event, user_session)

        @self.bot.on(events.CallbackQuery(pattern=r"folder_(\d+)"))
        async def callback_handler(event):
            user_id = event.sender_id
            user_session = await self.get_user_session(user_id)
            
            try:
                if not await user_session.ensure_authorized():
                    await event.answer("Требуется авторизация")
                    await self.start_auth_process(event, user_session)
                    return

                folder_id = int(event.data.decode().split('_')[1])
                folder_id_str = str(folder_id)
                
                async def get_folder_info():
                    dialog_filters = await user_session.client(GetDialogFiltersRequest())
                    return next((f for f in dialog_filters.filters if hasattr(f, 'id') and f.id == folder_id), None)
                
                folder = await user_session.handle_action(get_folder_info)
                if not folder:
                    await event.answer("Не удалось получить информацию о папке")
                    return

                # Загружаем все сохраненные данные пользователя
                data = self.load_user_data(user_id)
                folder_channels = data.get('folder_channels', {})

                if folder_id_str in user_session.active_folders:
                    # Деактивируем папку, но сохраняем информацию о канале
                    if folder_id in user_session.folder_handlers:
                        user_session.client.remove_event_handler(user_session.folder_handlers[folder_id])
                        del user_session.folder_handlers[folder_id]
                    
                    # Всегда сохраняем информацию о канале
                    if folder_id_str not in folder_channels:
                        folder_channels[folder_id_str] = {
                            'channel_id': user_session.active_folders[folder_id_str]['channel_id'],
                            'title': user_session.active_folders[folder_id_str]['title']
                        }
                    
                    del user_session.active_folders[folder_id_str]
                    await event.answer("Папка деактивирована")
                else:
                    try:
                        channel = None
                        # Всегда сначала пытаемся использовать существующий канал
                        if folder_id_str in folder_channels:
                            try:
                                channel_data = folder_channels[folder_id_str]
                                channel = await user_session.client.get_entity(channel_data['channel_id'])
                                logger.info(f"Используем существующий канал {channel.id} для папки {folder.title}")
                            except Exception as e:
                                logger.error(f"Не удалось получить существующий канал: {e}")
                                # Пробуем получить канал через get_input_entity
                                try:
                                    input_channel = await user_session.client.get_input_entity(channel_data['channel_id'])
                                    channel = await user_session.client.get_entity(input_channel)
                                    logger.info(f"Канал {channel.id} получен через input_entity")
                                except Exception as e2:
                                    logger.error(f"Не удалось получить канал через input_entity: {e2}")
                                    channel = None

                        # Создаем новый канал только если нет информации о существующем
                        if not channel and folder_id_str not in folder_channels:
                            channel = await self.create_folder_channel(user_session, folder.title)
                            if not channel:
                                await event.answer("Не удалось создать канал для папки")
                                return
                            # Сохраняем информацию о новом ка��але
                            folder_channels[folder_id_str] = {
                                'channel_id': channel.id,
                                'title': folder.title
                            }

                        if channel:
                            # Активируем папку
                            user_session.active_folders[folder_id_str] = {
                                'channel_id': channel.id,
                                'title': folder.title
                            }
                            
                            await self.setup_message_forwarding(user_session, folder, channel.id)
                            await event.answer("Папка активирована")
                        else:
                            await event.answer("Не удалось активировать папку: канал недоступен")
                            return

                    except Exception as e:
                        logger.error(f"Ошибка при активации папки: {e}")
                        await event.answer("Произошла ошибка при активации папки")
                        return

                # Сохраняем все данные
                self.save_user_data(user_id, {
                    'session_string': user_session.session_string,
                    'active_folders': user_session.active_folders,
                    'folder_channels': folder_channels
                })
                
                # Обновляем список папок
                await self.show_folders(event, user_session)
                
            except Exception as e:
                logger.error(f"Ошибка при обработке callback: {e}", exc_info=True)
                await event.answer("Произошла ошибка при обработке папки")

    async def cleanup_session(self, user_id):
        """Очистка сессии пользователя"""
        try:
            # Загружаем текущие данные
            data = self.load_user_data(user_id)
            # Очищаем только данные сессии, сохраняя информацию о каналах
            self.save_user_data(user_id, {
                'session_string': None,
                'active_folders': {},
                'folder_channels': data.get('folder_channels', {})  # Сохраняем информацию о каналах
            })
            logger.info(f"Очищена сессия пользователя {user_id}, информация о ка��алах сохранена")
        except Exception as e:
            logger.error(f"Ошибка при очистке сессии: {e}")

    async def start_auth_process(self, event, user_session):
        """Начало роцесса авторизации для пользователя"""
        try:
            # Проверяем, есть ли сохраненная сессия
            data = self.load_user_data(user_session.user_id)
            if data.get('session_string'):
                # Пробуем использовать существующую сессию
                user_session.session_string = data['session_string']
                if await user_session.init_client():
                    logger.info(f"Востанолена существующая сессия для пользователя {user_session.user_id}")
                    await self.show_folders(event, user_session)
                    return
            
            # Если нет сохраненной сессии или она недействительна, начинаем новую авторизацию
            user_session.client = TelegramClient(
                StringSession(),
                API_ID,
                API_HASH,
                device_model='Desktop',
                system_version='Windows 10',
                app_version='1.0',
                flood_sleep_threshold=60,
                request_retries=10,
                connection_retries=10,
                retry_delay=2,
                timeout=30,
                auto_reconnect=True
            )
            
            await user_session.client.connect()
            qr_login = await user_session.client.qr_login()
            
            # Создаем QR-код
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(qr_login.url)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            bio = BytesIO()
            bio.name = 'qr.png'
            img.save(bio, 'PNG')
            bio.seek(0)
            
            await event.respond(
                "Для авторизации:\n"
                "1. Откройте Telegram на телефоне\n"
                "2. Перейдите в Настройки -> Устройства -> Подключить устройство\n"
                "3. Отсканируйте этот QR-код",
                file=bio
            )
            
            # Ждем авторизацию
            await qr_login.wait()
            user_session.is_authorized = True
            
            # Сохраняем новую сессию
            user_session.session_string = user_session.client.session.save()
            self.save_user_data(user_session.user_id, {
                'session_string': user_session.session_string,
                'active_folders': user_session.active_folders,
                'folder_channels': data.get('folder_channels', {})
            })
            
            # Показываем список папок после авторизации
            await self.show_folders(event, user_session)
            
        except Exception as e:
            logger.error(f"Ошибка при авторизации пользователя {event.sender_id}: {e}")
            await event.respond("Произошла ошибка при авторизации. Попробуйте еще раз.")

    async def check_connections(self):
        """Периодическая проверка соединений"""
        while True:
            try:
                for user_id, session in self.users.items():
                    if session.is_authorized:
                        if not await session.ensure_connected():
                            logger.warning(f"Пользователь {user_id} потерял соединение")
                            # Попытка переинициализации клиента
                            if not await session.init_client():
                                logger.error(f"Не удалось переинициализировать клиент для пользователя {user_id}")
            except Exception as e:
                logger.error(f"Ошибка при проверке соединений: {e}", exc_info=True)
            await asyncio.sleep(15)  # Проверка каждые 15 секунд

    async def run(self):
        await self.setup()
        asyncio.create_task(self.check_connections())
        logger.info("Бот готов к работе")
        await self.bot.run_until_disconnected()

async def main():
    bot = TelegramBot()
    await bot.run()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)