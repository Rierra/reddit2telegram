import asyncpraw
import time
import requests
import hashlib
import os
import re
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, TimedOut, NetworkError, RetryAfter
import asyncio
import platform

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Fix for Windows event loop issues
if platform.system() == 'Windows':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

class PostUrlMonitor:
    def __init__(self):
        # Initialize Reddit API (Async PRAW) - FIX: Proper initialization
        self.reddit = None
        self.init_reddit()
        
        # Telegram config
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        # Initialize database for state tracking
        self.init_database()
        
        # Configuration for monitoring
        self.check_interval_seconds = int(os.getenv('CHECK_INTERVAL_SECONDS', '180'))  # 3 minutes
        self.max_requests_per_minute = int(os.getenv('MAX_REQUESTS_PER_MINUTE', '30'))
        self.delay_between_requests = float(os.getenv('DELAY_BETWEEN_REQUESTS', '2.0'))
        
        # Rate limiting protection
        self.requests_per_minute = 0
        self.last_minute_start = time.time()
        
        # Monitoring state
        self.monitoring_active = False
        self.monitoring_task = None
        
        # Telegram bot setup
        self.setup_telegram_bot()
    
    def init_reddit(self):
        """Initialize Reddit API properly"""
        try:
            self.reddit = asyncpraw.Reddit(
                client_id=os.getenv('REDDIT_CLIENT_ID'),
                client_secret=os.getenv('REDDIT_CLIENT_SECRET'),
                user_agent=os.getenv('REDDIT_USER_AGENT', 'PostUrlMonitor/1.0'),
                check_for_async=False  # FIX: Disable async check to avoid warnings
            )
            logger.info("Reddit API initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Reddit API: {e}")
            raise
        
    async def test_reddit_connection(self):
        """Test Reddit connection"""
        try:
            # Test with a simple subreddit fetch
            subreddit = await self.reddit.subreddit('test')
            await subreddit.load()
            logger.info("Reddit API connected successfully (read-only mode)")
        except Exception as e:
            logger.error(f"Reddit API connection failed: {e}")
            raise
        
    def setup_telegram_bot(self):
        """Setup Telegram bot handlers"""
        self.app = Application.builder().token(self.telegram_token).build()
        
        # Command handlers
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("add", self.add_url_command))
        self.app.add_handler(CommandHandler("list", self.list_urls_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("start_monitoring", self.start_monitoring_command))
        self.app.add_handler(CommandHandler("stop_monitoring", self.stop_monitoring_command))
        
        # Callback query handler for inline keyboards
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        
        # Message handler for URL input
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        # Global error handler
        self.app.add_error_handler(self.error_handler)
        
    def init_database(self):
        """Initialize SQLite database for tracking state - Enhanced with chat tracking"""
        try:
            self.conn = sqlite3.connect('reddit_post_monitor.db', check_same_thread=False)
            self.cursor = self.conn.cursor()
            
            # Create tables
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitored_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT UNIQUE,
                    subreddit TEXT,
                    url TEXT,
                    title TEXT,
                    created_utc INTEGER,
                    added_at INTEGER,
                    is_active INTEGER DEFAULT 1,
                    last_checked INTEGER,
                    chat_id TEXT,
                    chat_type TEXT,
                    added_by_user_id TEXT
                )
            ''')
            
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS comments (
                    comment_id TEXT PRIMARY KEY,
                    post_id TEXT,
                    subreddit TEXT,
                    author TEXT,
                    body TEXT,
                    score INTEGER,
                    created_utc INTEGER,
                    signature TEXT,
                    is_deleted INTEGER DEFAULT 0,
                    last_seen INTEGER,
                    FOREIGN KEY (post_id) REFERENCES monitored_posts (post_id)
                )
            ''')
            
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS best_comments (
                    post_id TEXT PRIMARY KEY,
                    comment_id TEXT,
                    signature TEXT,
                    author TEXT,
                    body_preview TEXT,
                    full_body TEXT,
                    score INTEGER,
                    last_updated INTEGER,
                    FOREIGN KEY (post_id) REFERENCES monitored_posts (post_id)
                )
            ''')
            
            # Add indexes
            self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_comments_post_id ON comments(post_id)')
            self.cursor.execute('CREATE INDEX IF NOT EXISTS idx_monitored_posts_active ON monitored_posts(is_active)')
            
            # Add new columns to existing table if they don't exist
            try:
                self.cursor.execute('ALTER TABLE monitored_posts ADD COLUMN chat_id TEXT')
                self.cursor.execute('ALTER TABLE monitored_posts ADD COLUMN chat_type TEXT') 
                self.cursor.execute('ALTER TABLE monitored_posts ADD COLUMN added_by_user_id TEXT')
            except sqlite3.OperationalError:
                # Columns already exist
                pass
            
            self.conn.commit()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise
    
    async def parse_reddit_url(self, url):
        """Parse Reddit URL to extract post information"""
        # Remove trailing slash and clean URL
        url = url.rstrip('/')
        
        # Pattern for Reddit post URLs
        patterns = [
            r'https?://(?:www\.)?reddit\.com/r/([^/]+)/comments/([^/]+)',
            r'https?://(?:www\.)?redd\.it/([^/\?]+)',
            r'https?://(?:old\.)?reddit\.com/r/([^/]+)/comments/([^/]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    return groups[0], groups[1]  # subreddit, post_id
                else:
                    # For redd.it links, we need to get the post info differently
                    post_id = groups[0]
                    try:
                        submission = await self.reddit.submission(id=post_id)
                        await submission.load()
                        subreddit = await submission.subreddit()
                        return subreddit.display_name, post_id
                    except Exception as e:
                        logger.error(f"Error parsing redd.it URL: {e}")
                        return None, None
        
        return None, None
    
    def is_reddit_url(self, url):
        """Check if URL is a Reddit URL"""
        patterns = [
            r'https?://(?:www\.)?reddit\.com/r/[^/]+/comments/[^/]+',
            r'https?://(?:www\.)?redd\.it/[^/\?]+',
            r'https?://(?:old\.)?reddit\.com/r/[^/]+/comments/[^/]+'
        ]
        
        for pattern in patterns:
            if re.search(pattern, url):
                return True
        return False
    
    async def add_post_url(self, url, chat_id=None, chat_type=None, user_id=None):
        """Add a post URL to monitoring list - Enhanced with chat tracking"""
        try:
            subreddit, post_id = await self.parse_reddit_url(url)
            
            if not subreddit or not post_id:
                return False, "Invalid Reddit URL format"

            # Get post information
            submission = await self.reddit.submission(id=post_id)
            await submission.load()  # Load submission data
            
            # Check if post already exists AND is active
            self.cursor.execute('SELECT id, is_active, chat_id FROM monitored_posts WHERE post_id = ?', (post_id,))
            existing_post = self.cursor.fetchone()
            
            if existing_post and existing_post[1] == 1:  # Check if active
                return False, f"Post is already being monitored (alerts go to chat: {existing_post[2]})"
            elif existing_post and existing_post[1] == 0:  # Reactivate if inactive
                self.cursor.execute('''
                    UPDATE monitored_posts 
                    SET is_active = 1, added_at = ?, url = ?, title = ?, chat_id = ?, chat_type = ?, added_by_user_id = ?
                    WHERE post_id = ?
                ''', (int(time.time()), url, submission.title, str(chat_id), chat_type, str(user_id), post_id))
                self.conn.commit()
                return True, f"Reactivated monitoring for post from r/{subreddit}: {submission.title[:50]}..."
            
            # Add new post to database with chat info
            self.cursor.execute('''
                INSERT INTO monitored_posts 
                (post_id, subreddit, url, title, created_utc, added_at, is_active, chat_id, chat_type, added_by_user_id)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ''', (
                post_id,
                subreddit,
                url,
                submission.title,
                int(submission.created_utc),
                int(time.time()),
                str(chat_id),
                chat_type,
                str(user_id)
            ))
            
            self.conn.commit()
            return True, f"Added post from r/{subreddit}: {submission.title[:50]}... (alerts will be sent to this chat)"
            
        except Exception as e:
            logger.error(f"Error adding post URL {url}: {e}")
            return False, f"Error accessing post: {str(e)}"
    
    def get_monitored_posts(self, chat_id=None):
        """Get monitored posts - optionally filter by chat_id"""
        try:
            if chat_id:
                self.cursor.execute('''
                    SELECT post_id, subreddit, title, url, added_at, last_checked, chat_id, chat_type
                    FROM monitored_posts 
                    WHERE is_active = 1 AND chat_id = ?
                    ORDER BY added_at DESC
                ''', (str(chat_id),))
            else:
                self.cursor.execute('''
                    SELECT post_id, subreddit, title, url, added_at, last_checked, chat_id, chat_type
                    FROM monitored_posts 
                    WHERE is_active = 1
                    ORDER BY added_at DESC
                ''')
            return self.cursor.fetchall()
        except Exception as e:
            logger.error(f"Error getting monitored posts: {e}")
            return []
    
    def get_post_chat_id(self, post_id):
        """Get the chat_id where alerts should be sent for a specific post"""
        try:
            self.cursor.execute('SELECT chat_id FROM monitored_posts WHERE post_id = ? AND is_active = 1', (post_id,))
            result = self.cursor.fetchone()
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting chat_id for post {post_id}: {e}")
            return None
    
    def remove_post(self, post_id):
        """Remove a post from monitoring"""
        try:
            self.cursor.execute('UPDATE monitored_posts SET is_active = 0 WHERE post_id = ?', (post_id,))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing post {post_id}: {e}")
            return False
    
    # Telegram Bot Commands
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_message = (
            "🤖 <b>Reddit Post Monitor Bot</b>\n\n"
            "I can monitor specific Reddit posts for:\n"
            "• New comments\n"
            "• Deleted comments\n"
            "• Best comment changes\n\n"
            "Use /help to see all commands."
        )
        await update.message.reply_text(welcome_message, parse_mode='HTML')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = (
            "<b>Available Commands:</b>\n\n"
            "/add - Add a Reddit post URL to monitor\n"
            "/list - View all monitored posts\n"
            "/status - Check monitoring status\n"
            "/start_monitoring - Start monitoring posts\n"
            "/stop_monitoring - Stop monitoring\n"
            "/help - Show this help message\n\n"
            "<b>How to use:</b>\n"
            "1. Use /add and paste a Reddit post URL\n"
            "2. Use /start_monitoring to begin monitoring\n"
            "3. You'll get alerts for any changes!"
        )
        await update.message.reply_text(help_text, parse_mode='HTML')
    
    async def add_url_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /add command"""
        context.user_data['waiting_for_url'] = True
        await update.message.reply_text(
            "📝 Please send me the Reddit post URL you want to monitor.\n\n"
            "Supported formats:\n"
            "• https://reddit.com/r/subreddit/comments/post_id/\n"
            "• https://redd.it/post_id\n"
            "• https://old.reddit.com/r/subreddit/comments/post_id/"
        )
    
    async def list_urls_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command - Show posts for current chat with pagination"""
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        
        # Show posts for current chat
        posts = self.get_monitored_posts(chat_id)
        
        if not posts:
            await update.message.reply_text("📭 No posts are currently being monitored in this chat.")
            return
        
        # Build individual post messages and split into chunks
        MAX_MESSAGE_LENGTH = 4000  # Leave some room for safety (Telegram limit is 4096)
        
        header = f"<b>📋 Monitored Posts in this {chat_type}:</b>\n\n"
        messages = []
        current_message = header
        
        for i, (post_id, subreddit, title, url, added_at, last_checked, _, _) in enumerate(posts, 1):
            added_date = datetime.fromtimestamp(added_at).strftime('%Y-%m-%d %H:%M')
            last_check = "Never" if not last_checked else datetime.fromtimestamp(last_checked).strftime('%H:%M:%S')
            
            post_entry = (
                f"{i}. <b>r/{subreddit}</b>\n"
                f"   {title[:50]}{'...' if len(title) > 50 else ''}\n"
                f"   Added: {added_date} | Last: {last_check}\n"
                f"   <a href='{url}'>View Post</a>\n\n"
            )
            
            # Check if adding this entry would exceed the limit
            if len(current_message) + len(post_entry) > MAX_MESSAGE_LENGTH:
                messages.append(current_message)
                current_message = post_entry  # Start new message with this entry
            else:
                current_message += post_entry
        
        # Add the last message
        if current_message:
            messages.append(current_message)
        
        # Create inline keyboard for post management (split into chunks too)
        keyboard = []
        for post_id, subreddit, title, url, _, _, _, _ in posts:
            # Shorten button text to avoid issues
            short_title = title[:20] + '...' if len(title) > 20 else title
            keyboard.append([InlineKeyboardButton(
                f"🗑️ r/{subreddit[:10]}: {short_title}",
                callback_data=f"remove_{post_id}"
            )])
        
        # Send messages
        try:
            for i, msg in enumerate(messages):
                if i == len(messages) - 1 and len(keyboard) <= 10:
                    # Last message - attach keyboard if not too large
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=reply_markup)
                else:
                    await update.message.reply_text(msg, parse_mode='HTML')
                await asyncio.sleep(0.5)  # Small delay between messages
            
            # If we have more than 10 posts, send keyboard separately
            if len(keyboard) > 10:
                await update.message.reply_text(
                    "📝 <b>Manage posts:</b>",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard[:50])  # Limit to 50 buttons
                )
        except BadRequest as e:
            if "too long" in str(e).lower():
                # Fallback: send very minimal info
                await update.message.reply_text(
                    f"📋 You have {len(posts)} posts monitored. "
                    f"Use the buttons below to manage them.",
                    reply_markup=InlineKeyboardMarkup(keyboard[:20])
                )
            else:
                raise
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        posts_count = len(self.get_monitored_posts())
        status = "🟢 Running" if self.monitoring_active else "🔴 Stopped"
        
        message = (
            f"<b>📊 Monitor Status</b>\n\n"
            f"Status: {status}\n"
            f"Monitored posts: {posts_count}\n"
            f"Check interval: {self.check_interval_seconds} seconds\n"
            f"Rate limit: {self.max_requests_per_minute} requests/minute\n"
        )
        
        if self.monitoring_active:
            message += f"\nMonitoring started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        await update.message.reply_text(message, parse_mode='HTML')
    
    async def start_monitoring_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start_monitoring command"""
        if self.monitoring_active:
            await update.message.reply_text("⚠️ Monitoring is already running!")
            return
        
        posts = self.get_monitored_posts()
        if not posts:
            await update.message.reply_text("❌ No posts to monitor. Add some posts first with /add")
            return
        
        self.monitoring_active = True
        # Store the task reference so we can cancel it later
        self.monitoring_task = asyncio.create_task(self.run_monitoring_loop())
        
        await update.message.reply_text(
            f"✅ Monitoring started!\n"
            f"Watching {len(posts)} posts every {self.check_interval_seconds} seconds."
        )
    
    async def stop_monitoring_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop_monitoring command"""
        if not self.monitoring_active:
            await update.message.reply_text("⚠️ Monitoring is not running!")
            return
        
        self.monitoring_active = False
        
        # Cancel the monitoring task properly
        if self.monitoring_task and not self.monitoring_task.done():
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass  # Expected when cancelling
        
        self.monitoring_task = None
        await update.message.reply_text("🛑 Monitoring stopped!")
    
    async def run_monitoring_loop(self):
        """Run the monitoring loop"""
        logger.info("Starting monitoring loop...")
        
        try:
            while self.monitoring_active:
                try:
                    await self.run_monitoring_cycle()
                    await asyncio.sleep(self.check_interval_seconds)
                except asyncio.CancelledError:
                    logger.info("Monitoring loop cancelled by user")
                    break
                except Exception as e:
                    logger.error(f"Error in monitoring loop: {e}")
                    # Send error notification to default chat
                    error_message = f"❌ <b>Monitor Error</b>\n{str(e)}\n\nRetrying in 2 minutes..."
                    await self.send_telegram_alert_async(error_message, self.chat_id)
                    await asyncio.sleep(120)
        except asyncio.CancelledError:
            logger.info("Monitoring loop cancelled")
        finally:
            self.monitoring_active = False
            logger.info("Monitoring loop stopped")
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses"""
        query = update.callback_query
        await query.answer()
        
        if query.data.startswith('remove_'):
            post_id = query.data.replace('remove_', '')
            if self.remove_post(post_id):
                await query.edit_message_text("✅ Post removed from monitoring.")
            else:
                await query.edit_message_text("❌ Failed to remove post.")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages (for URL input) - Fixed for groups with chat tracking"""
        
        # Get the message text and chat info
        message_text = update.effective_message.text.strip()
        chat_type = update.effective_chat.type
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Log for debugging
        logger.info(f"Message received in {chat_type} (ID: {chat_id}): '{message_text}' from user {user_id}")
        
        # Check if user is waiting for URL OR if message contains Reddit URL
        is_waiting_for_url = context.user_data.get('waiting_for_url', False)
        contains_reddit_url = self.is_reddit_url(message_text)
        
        if is_waiting_for_url or contains_reddit_url:
            # Process the URL with chat information
            success, message = await self.add_post_url(message_text, chat_id, chat_type, user_id)
            
            if success:
                await update.message.reply_text(f"✅ {message}")
            else:
                await update.message.reply_text(f"❌ {message}")
            
            # Clear the waiting state
            context.user_data['waiting_for_url'] = False
            
        elif is_waiting_for_url and not contains_reddit_url:
            # They were waiting for URL but sent something else
            await update.message.reply_text(
                "❌ That doesn't look like a Reddit URL. Please send a valid Reddit post URL.\n"
                "Supported formats:\n"
                "• https://reddit.com/r/subreddit/comments/post_id/\n"
                "• https://redd.it/post_id\n"
                "• https://old.reddit.com/r/subreddit/comments/post_id/"
            )
        else:
            # Only respond with help in DMs or if directly mentioned/replied to
            if chat_type == 'private':
                await update.message.reply_text(
                    "I don't understand that message. Use /help to see available commands."
                )
    
    async def send_telegram_alert_to_chat(self, message, chat_id):
        """Send alert to specific chat (async version)"""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=payload, timeout=10) as response:
                    response.raise_for_status()
                    logger.info(f"Telegram alert sent successfully to chat {chat_id}")
                    return True
        except Exception as e:
            logger.error(f"Failed to send async Telegram alert to chat {chat_id}: {e}")
            return False

    def send_telegram_alert_to_chat_sync(self, message, chat_id):
        """Send alert to specific chat (sync version)"""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        
        try:
            response = requests.post(url, data=payload, timeout=10)
            response.raise_for_status()
            logger.info(f"Telegram alert sent successfully to chat {chat_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send Telegram alert to chat {chat_id}: {e}")
            return False
    
    # FIX: Add async version of telegram alert with retry logic
    async def send_telegram_alert_async(self, message, chat_id, max_retries=3):
        """Send alert via Telegram bot (async version with retry)"""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        
        import aiohttp
        
        for attempt in range(max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=30)  # Increased timeout
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, data=payload) as response:
                        if response.status == 200:
                            logger.info(f"Telegram alert sent successfully to chat {chat_id}")
                            return True
                        elif response.status == 429:  # Rate limited
                            retry_after = int(response.headers.get('Retry-After', 5))
                            logger.warning(f"Rate limited by Telegram. Waiting {retry_after}s...")
                            await asyncio.sleep(retry_after)
                        else:
                            response.raise_for_status()
            except asyncio.TimeoutError:
                wait_time = (attempt + 1) * 2  # Exponential backoff: 2, 4, 6 seconds
                logger.warning(f"Telegram request timed out (attempt {attempt + 1}/{max_retries}). Retrying in {wait_time}s...")
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
            except aiohttp.ClientError as e:
                wait_time = (attempt + 1) * 2
                logger.warning(f"Network error (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                if attempt < max_retries - 1:
                    await asyncio.sleep(wait_time)
            except Exception as e:
                logger.error(f"Failed to send async Telegram alert: {e}")
                return False
        
        logger.error(f"Failed to send Telegram alert after {max_retries} attempts")
        return False
    
    def send_telegram_alert(self, message):
        """Send alert via Telegram bot (sync version)"""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        
        try:
            response = requests.post(url, data=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram alert sent successfully")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send Telegram alert: {e}")
            return False
    
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler for the Telegram bot"""
        error = context.error
        
        # Log the error
        logger.error(f"Exception while handling an update: {error}")
        
        # Handle specific error types
        if isinstance(error, BadRequest):
            if "message is too long" in str(error).lower():
                logger.warning("Telegram message was too long, message truncated or split failed")
                if update and hasattr(update, 'effective_message') and update.effective_message:
                    try:
                        await update.effective_message.reply_text(
                            "⚠️ The response was too long. Please try a more specific request."
                        )
                    except Exception:
                        pass
            else:
                logger.error(f"BadRequest error: {error}")
        
        elif isinstance(error, TimedOut):
            logger.warning(f"Request timed out: {error}. This is usually temporary.")
        
        elif isinstance(error, NetworkError):
            logger.warning(f"Network error: {error}. Will retry automatically.")
        
        elif isinstance(error, RetryAfter):
            logger.warning(f"Rate limited by Telegram. Retry after {error.retry_after} seconds.")
            await asyncio.sleep(error.retry_after)
        
        else:
            logger.error(f"Unhandled error: {type(error).__name__}: {error}")
    
    async def rate_limit_check(self):
        """Check and enforce rate limiting (async version)"""
        current_time = time.time()
        
        if current_time - self.last_minute_start >= 60:
            self.requests_per_minute = 0
            self.last_minute_start = current_time
        
        if self.requests_per_minute >= self.max_requests_per_minute:
            wait_time = 60 - (current_time - self.last_minute_start)
            logger.warning(f"Rate limit reached. Waiting {wait_time:.1f} seconds...")
            await asyncio.sleep(wait_time + 1)
            self.requests_per_minute = 0
            self.last_minute_start = time.time()
        
        self.requests_per_minute += 1
    
    def get_comment_signature(self, comment):
        """Create a unique signature for a comment"""
        try:
            if hasattr(comment, 'body') and hasattr(comment, 'id') and comment.body:
                return f"{comment.id}_{hashlib.md5(comment.body.encode()).hexdigest()[:8]}"
        except Exception as e:
            logger.error(f"Error creating comment signature: {e}")
        return None
    
    def get_reddit_link(self, subreddit_name, post_id, comment_id=None):
        """Generate Reddit links"""
        base_url = f"https://www.reddit.com/r/{subreddit_name}/comments/{post_id}/"
        if comment_id:
            return f"{base_url}_/{comment_id}/"
        return base_url
    
    def truncate_text(self, text, max_length=200):
        """Truncate text for preview"""
        if not text:
            return ""
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."
    
    def get_stored_comments(self, post_id):
        """Get previously stored comments for a post"""
        try:
            self.cursor.execute('''
                SELECT comment_id, author, body, score, created_utc, signature, is_deleted
                FROM comments WHERE post_id = ? AND is_deleted = 0
            ''', (post_id,))
            
            comments = {}
            for row in self.cursor.fetchall():
                comments[row[0]] = {
                    'id': row[0],
                    'author': row[1],
                    'body': row[2],
                    'score': row[3],
                    'created_utc': row[4],
                    'signature': row[5],
                    'is_deleted': row[6]
                }
            return comments
        except Exception as e:
            logger.error(f"Error getting stored comments for {post_id}: {e}")
            return {}
    
    async def get_current_comments(self, submission):
        """Get current comments from submission with rate limiting"""
        try:
            await self.rate_limit_check()
            
            # FIX: Proper comment tree handling
            submission.comment_limit = None
            submission.comment_sort = 'new'
            await submission.comments.replace_more(limit=None)
            comments = {}
            
            def process_comment_tree(comment_list):
                for comment in comment_list:
                    # FIX: Better comment validation
                    if (hasattr(comment, 'body') and hasattr(comment, 'id') and 
                        comment.body and comment.body != '[deleted]' and 
                        comment.body != '[removed]'):
                        
                        comments[comment.id] = {
                            'id': comment.id,
                            'author': str(comment.author) if comment.author else '[deleted]',
                            'body': comment.body,
                            'score': getattr(comment, 'score', 0),
                            'created_utc': int(getattr(comment, 'created_utc', 0)),
                            'signature': self.get_comment_signature(comment)
                        }
                        
                        # Process replies
                        if hasattr(comment, 'replies') and comment.replies:
                            process_comment_tree(comment.replies)
            
            process_comment_tree(submission.comments)
            return comments
            
        except Exception as e:
            logger.error(f"Error getting current comments for {submission.id}: {e}")
            if "429" in str(e) or "rate" in str(e).lower():
                logger.warning("Rate limit hit while getting comments, waiting...")
                await asyncio.sleep(30)
            return {}
    
    async def get_best_comment(self, submission):
        """Get the top comment when sorted by 'best' with rate limiting"""
        try:
            await self.rate_limit_check()
            submission.comment_sort = 'best'
            await submission.comments.replace_more(limit=0)
            
            if submission.comments and len(submission.comments) > 0:
                best_comment = submission.comments[0]
                if (hasattr(best_comment, 'body') and best_comment.body and 
                    best_comment.body not in ['[deleted]', '[removed]']):
                    
                    return {
                        'id': best_comment.id,
                        'author': str(best_comment.author) if best_comment.author else '[deleted]',
                        'body': best_comment.body,
                        'body_preview': self.truncate_text(best_comment.body, 150),
                        'score': getattr(best_comment, 'score', 0),
                        'signature': self.get_comment_signature(best_comment),
                        'created_utc': int(getattr(best_comment, 'created_utc', 0))
                    }
        except Exception as e:
            logger.error(f"Error getting best comment for {submission.id}: {e}")
            if "429" in str(e) or "rate" in str(e).lower():
                logger.warning("Rate limit hit while getting best comment, waiting...")
                await asyncio.sleep(30)
        return None
    
    def store_comments(self, post_id, subreddit_name, comments):
        """Store current comments in database"""
        try:
            current_time = int(time.time())
            
            for comment_id, comment_data in comments.items():
                self.cursor.execute('''
                    INSERT OR REPLACE INTO comments 
                    (comment_id, post_id, subreddit, author, body, score, created_utc, signature, is_deleted, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ''', (
                    comment_id,
                    post_id,
                    subreddit_name,
                    comment_data['author'],
                    comment_data['body'],
                    comment_data['score'],
                    comment_data['created_utc'],
                    comment_data['signature'],
                    current_time
                ))
            
            self.conn.commit()
        except Exception as e:
            logger.error(f"Error storing comments for {post_id}: {e}")
    
    def get_stored_best_comment(self, post_id):
        """Get previously stored best comment for a post"""
        try:
            self.cursor.execute('''
                SELECT comment_id, signature, author, body_preview, full_body, score
                FROM best_comments WHERE post_id = ?
            ''', (post_id,))
            row = self.cursor.fetchone()
            if row:
                return {
                    'id': row[0],
                    'signature': row[1],
                    'author': row[2],
                    'body_preview': row[3],
                    'full_body': row[4],
                    'score': row[5]
                }
        except Exception as e:
            logger.error(f"Error getting stored best comment for {post_id}: {e}")
        return None
    
    def store_best_comment(self, post_id, best_comment):
        """Store best comment information"""
        try:
            if best_comment:
                self.cursor.execute('''
                    INSERT OR REPLACE INTO best_comments 
                    (post_id, comment_id, signature, author, body_preview, full_body, score, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    post_id,
                    best_comment['id'],
                    best_comment['signature'],
                    best_comment['author'],
                    best_comment['body_preview'],
                    best_comment['body'],
                    best_comment['score'],
                    int(time.time())
                ))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Error storing best comment for {post_id}: {e}")
    
    def mark_comments_as_deleted(self, post_id, deleted_comment_ids):
        """Mark comments as deleted in database"""
        try:
            for comment_id in deleted_comment_ids:
                self.cursor.execute('''
                    UPDATE comments SET is_deleted = 1 WHERE comment_id = ? AND post_id = ?
                ''', (comment_id, post_id))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Error marking comments as deleted for {post_id}: {e}")
    
    async def monitor_post(self, post_id, subreddit_name, title):
        """Monitor a single post for real-time changes"""
        try:
            # Get the chat_id where alerts should be sent for this post
            target_chat_id = self.get_post_chat_id(post_id)
            if not target_chat_id:
                logger.warning(f"No target chat found for post {post_id}, skipping")
                return 0

            # Get submission
            submission = await self.reddit.submission(id=post_id)
            await submission.load()
            
            # Update last checked time
            self.cursor.execute('''
                UPDATE monitored_posts SET last_checked = ? WHERE post_id = ?
            ''', (int(time.time()), post_id))
            self.conn.commit()
            
            # Get current and stored states
            current_comments = await self.get_current_comments(submission)
            stored_comments = self.get_stored_comments(post_id)
            current_best_comment = await self.get_best_comment(submission)
            stored_best_comment = self.get_stored_best_comment(post_id)
            
            alerts = []
            
            # Check for new comments
            new_comment_ids = set(current_comments.keys()) - set(stored_comments.keys())
            for comment_id in new_comment_ids:
                comment = current_comments[comment_id]
                comment_link = self.get_reddit_link(subreddit_name, post_id, comment_id)
                post_link = self.get_reddit_link(subreddit_name, post_id)
                
                comment_time = datetime.fromtimestamp(comment['created_utc']).strftime('%H:%M:%S')
                
                alert = (
                    f"💬 <b>New Comment</b> in r/{subreddit_name}\n"
                    f"📝 Post: <i>{self.truncate_text(title, 40)}</i>\n"
                    f"👤 Author: {comment['author']}\n"
                    f"🕐 Time: {comment_time}\n"
                    f"⭐ Score: {comment['score']}\n"
                    f"💭 Content:\n<i>{self.truncate_text(comment['body'], 300)}</i>\n"
                    f"🔗 <a href='{comment_link}'>View Comment</a> | <a href='{post_link}'>View Post</a>"
                )
                alerts.append(alert)
            
            # Check for removed comments
            removed_comment_ids = set(stored_comments.keys()) - set(current_comments.keys())
            for comment_id in removed_comment_ids:
                removed_comment = stored_comments[comment_id]
                post_link = self.get_reddit_link(subreddit_name, post_id)
                
                alert = (
                    f"🗑️ <b>Comment Removed</b> from r/{subreddit_name}\n"
                    f"📝 Post: <i>{self.truncate_text(title, 40)}</i>\n"
                    f"👤 Author: {removed_comment['author']}\n"
                    f"⭐ Score: {removed_comment['score']}\n"
                    f"💭 Removed Content:\n<i>{self.truncate_text(removed_comment['body'], 300)}</i>\n"
                    f"🔗 <a href='{post_link}'>View Post</a>"
                )
                alerts.append(alert)
            
            # Mark removed comments as deleted
            if removed_comment_ids:
                self.mark_comments_as_deleted(post_id, removed_comment_ids)
            
            # Check for best comment changes
            if stored_best_comment and current_best_comment:
                if (stored_best_comment['signature'] != current_best_comment['signature'] and 
                    stored_best_comment['signature'] is not None):
                    
                    comment_link = self.get_reddit_link(subreddit_name, post_id, current_best_comment['id'])
                    post_link = self.get_reddit_link(subreddit_name, post_id)
                    
                    alert = (
                        f"🏆 <b>Best Comment Changed!</b> in r/{subreddit_name}\n"
                        f"📝 Post: <i>{self.truncate_text(title, 40)}</i>\n"
                        f"👤 New top: {current_best_comment['author']}\n"
                        f"⭐ Score: {current_best_comment['score']}\n"
                        f"💭 New best comment:\n<i>{current_best_comment['body_preview']}</i>\n"
                        f"🔗 <a href='{comment_link}'>View Comment</a> | <a href='{post_link}'>View Post</a>"
                    )
                    alerts.append(alert)
            elif not stored_best_comment and current_best_comment:
                comment_link = self.get_reddit_link(subreddit_name, post_id, current_best_comment['id'])
                post_link = self.get_reddit_link(subreddit_name, post_id)
                
                alert = (
                    f"🎉 <b>First Comment!</b> in r/{subreddit_name}\n"
                    f"📝 Post: <i>{self.truncate_text(title, 40)}</i>\n"
                    f"👤 Author: {current_best_comment['author']}\n"
                    f"💭 Content:\n<i>{current_best_comment['body_preview']}</i>\n"
                    f"🔗 <a href='{comment_link}'>View Comment</a> | <a href='{post_link}'>View Post</a>"
                )
                alerts.append(alert)
            
            # Store current state
            self.store_comments(post_id, subreddit_name, current_comments)
            if current_best_comment:
                self.store_best_comment(post_id, current_best_comment)
            
            # Send alerts to the correct chat
            for alert in alerts:
                await self.send_telegram_alert_async(alert, target_chat_id)  # ✅ Fixed: Send to specific chat
                await asyncio.sleep(1)
            
            return len(alerts)
            
        except Exception as e:
            logger.error(f"Error monitoring post {post_id}: {e}")
            return 0
    
    async def run_monitoring_cycle(self):
        """Run a single monitoring cycle for all posts"""
        posts = self.get_monitored_posts()
        
        if not posts:
            return
        
        total_alerts = 0
        
        for post_id, subreddit, title, url, _, _, _, _ in posts:
            try:
                alerts_count = await self.monitor_post(post_id, subreddit, title)
                total_alerts += alerts_count
                await asyncio.sleep(self.delay_between_requests)
                
            except Exception as e:
                logger.error(f"Error processing post {post_id}: {e}")
                continue
        
        if total_alerts > 0:
            logger.info(f"Monitoring cycle completed: {total_alerts} alerts sent for {len(posts)} posts")
    

    
    async def run_telegram_bot(self):
          """Run the Telegram bot"""
          logger.info("Starting Telegram bot...")
          await self.app.initialize()
          await self.app.start()
          await self.app.updater.start_polling()
          
          # Send startup notification
          startup_message = (
              f"🚀 <b>Reddit Post Monitor Started!</b>\n"
              f"🕐 Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"⏱️ Check interval: {self.check_interval_seconds} seconds\n"
              f"🛡️ Rate limit: {self.max_requests_per_minute} req/min\n\n"
              f"Use /help to see available commands."
          )
          self.send_telegram_alert(startup_message)
          
          try:
              # Keep the bot running - use a simple loop instead of idle()
              while True:
                  await asyncio.sleep(1)
          except KeyboardInterrupt:
              logger.info("Bot stopped by user")
          finally:
              # Proper shutdown sequence
              await self.app.updater.stop()  # Stop updater first
              await self.app.stop()          # Then stop application
              await self.app.shutdown()      # Finally shutdown
              self.conn.close()

# Main execution
async def main():
    # Validate required environment variables
    required_vars = ['REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return
    
    # Create and run monitor
    monitor = PostUrlMonitor()
    await monitor.run_telegram_bot()

if __name__ == "__main__":
    asyncio.run(main())
