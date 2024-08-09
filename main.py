import os
import subprocess
import logging
from datetime import datetime, timedelta
import asyncio
import aiohttp
import psutil
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackContext
import aiocron
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8000)

def keep_alive():
    t = Thread(target=run)
    t.start()

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

bot_token = '7263108248:AAGx-g0wBMSiXAz9I646PS1gRoNWweiJjW4'
bot = Bot(token=bot_token)
recording_durations = {}  # Store recording durations for each chat
scheduled_jobs = {}  # Store scheduled jobs for each chat

DOWNLOAD_DIR = "downloads"
API_KEY = '46topBnuEaqZ5FRf'
API_URL = 'https://www.playerx.stream/api.php'
MAX_VIDEO_SIZE = 10 * 1024 * 1024 * 1024  # 10GB

# Ensure the download directory exists
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        'Send me an .m3u8 link to start recording.\n'
        'Use /set_duration <seconds> to set recording duration.\n'
        'Available options: 10 (default), 600 (10 min), 1200 (20 min), 1800 (30 min).\n'
        'Use /schedule <name> <HH:MMAM/PM> <duration in minutes> <url> to schedule a recording.\n'
        'Use /reschedule <name> <HH:MMAM/PM> <duration in minutes> <url> to reschedule a recording.\n'
        'Use /list_schedules to list all scheduled recordings.\n'
        'Use /cancel_schedule <name> to cancel a scheduled recording.\n'
        'Use /status to check storage, RAM, and CPU usage.\n'
        'Use /delete to delete all stored videos.\n'
        'Use /ls to list all files in the download directory.\n'
        'Use /info <filename> to view file info like size and last modified date.\n'
        'Use /upload <filename> to upload that file to PlayerX.'
    )

async def set_duration(update: Update, context: CallbackContext):
    chat_id = update.message.chat.id
    try:
        duration = int(context.args[0])
        if duration in [10, 600, 1200, 1800]:
            recording_durations[chat_id] = duration
            await update.message.reply_text(f"Recording duration set to {duration} seconds.")
        else:
            await update.message.reply_text("Invalid duration. Available options: 10, 600, 1200, 1800 seconds.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_duration <seconds>")

async def record_stream(update: Update, context: CallbackContext):
    chat_id = update.message.chat.id
    message = update.message.text

    if message.startswith('http'):
        duration = recording_durations.get(chat_id, 10)  # Default to 10 seconds if not set
        await update.message.reply_text(f"Recording started for {duration} seconds...")
        m3u8_url = message
        output_file = os.path.join(DOWNLOAD_DIR, f'{chat_id}.mp4')

        if os.path.exists(output_file):
            await update.message.reply_text("File already exists. Please try a different name.")
            return

        command = ['ffmpeg', '-i', m3u8_url, '-c', 'copy', '-t', str(duration), output_file]
        try:
            process = subprocess.Popen(command)
            while process.poll() is None:
                await bot.send_message(chat_id=chat_id, text="Recording in progress...")
                await asyncio.sleep(duration // 2)  # Send progress update halfway through the recording
            await update.message.reply_text("Recording finished. Uploading video...")

            await upload_and_send_file(chat_id, output_file, duration)
        except subprocess.CalledProcessError as e:
            logger.error(f"Recording failed: {e}")
            await update.message.reply_text("Recording failed. Please try again.")
    else:
        await update.message.reply_text("Please send a valid .m3u8 link.")

async def upload_and_send_file(chat_id, file_path, duration):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                data={
                    'files[]': open(file_path, 'rb'),
                    'api_key': API_KEY,
                    'action': 'upload_video',
                    'raw': '0'
                }
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    await bot.send_message(chat_id=chat_id, text=f"File uploaded successfully. Result: {result}")
                else:
                    await bot.send_message(chat_id=chat_id, text="Failed to upload the video to the API Gateway.")

        await bot.send_document(chat_id=chat_id, document=open(file_path, 'rb'))
        os.remove(file_path)
    except Exception as e:
        logger.error(f"Error during file upload: {e}")
        await bot.send_message(chat_id=chat_id, text="Failed to upload the video. Please try again.")

async def schedule_recording(update: Update, context: CallbackContext):
    chat_id = update.message.chat.id
    try:
        name = context.args[0]
        time_str = context.args[1]
        duration_min = int(context.args[2])
        url = context.args[3]

        if chat_id in scheduled_jobs and any(job_name == name for job_name, _, _ in scheduled_jobs[chat_id]):
            await update.message.reply_text(f"Recording name '{name}' is already scheduled. Choose a different name.")
            return

        # Parse time string and calculate the next occurrence
        now = datetime.now()
        schedule_time = datetime.strptime(time_str, "%I:%M%p").replace(year=now.year, month=now.month, day=now.day)
        if schedule_time < now:
            schedule_time += timedelta(days=1)

        duration_sec = duration_min * 60
        notify_time = schedule_time - timedelta(minutes=5)  # Notify 5 minutes before the recording

        # Log the scheduled time in seconds since epoch
        scheduled_timestamp = int(schedule_time.timestamp())
        logger.info(f"Scheduled recording '{name}' at {schedule_time.strftime('%Y-%m-%d %H:%M:%S')} ({scheduled_timestamp} seconds since epoch).")

        # Schedule the recording
        cron_expression = f'{schedule_time.minute} {schedule_time.hour} * * *'
        job = aiocron.crontab(cron_expression, func=record_scheduled_stream, args=(chat_id, name, url, duration_sec))
        notify_cron_expression = f'{notify_time.minute} {notify_time.hour} * * *'
        notify_job = aiocron.crontab(notify_cron_expression, func=notify_user, args=(chat_id, name))

        if chat_id not in scheduled_jobs:
            scheduled_jobs[chat_id] = []
        scheduled_jobs[chat_id].append((name, job, notify_job))
        
        await update.message.reply_text(f"Scheduled recording '{name}' at {time_str} for {duration_min} minutes.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /schedule <name> <HH:MMAM/PM> <duration in minutes> <url>")
    except Exception as e:
        logger.error(f"Error scheduling recording: {e}")
        await update.message.reply_text("Failed to schedule recording. Please try again.")

async def notify_user(chat_id, name):
    await bot.send_message(chat_id=chat_id, text=f"Scheduled recording '{name}' will start in 5 minutes.")

async def record_scheduled_stream(chat_id, name, url, duration):
    output_file = os.path.join(DOWNLOAD_DIR, f'{name}.mp4')
    command = ['ffmpeg', '-i', url, '-c', 'copy', '-t', str(duration), output_file]
    try:
        process = subprocess.Popen(command)
        while process.poll() is None:
            await bot.send_message(chat_id=chat_id, text=f"Scheduled recording '{name}' in progress...")
            await asyncio.sleep(duration // 2)  # Send progress update halfway through the recording
        await bot.send_message(chat_id=chat_id, text=f"Scheduled recording '{name}' finished. Uploading video...")

        await upload_and_send_file(chat_id, output_file, duration)
        
        # Remove the completed job from the scheduled list
        if chat_id in scheduled_jobs:
            scheduled_jobs[chat_id] = [job for job in scheduled_jobs[chat_id] if job[0] != name]
    except subprocess.CalledProcessError as e:
        logger.error(f"Scheduled recording failed: {e}")
        await bot.send_message(chat_id=chat_id, text=f"Scheduled recording '{name}' failed. Please try again.")

async def reschedule_recording(update: Update, context: CallbackContext):
    chat_id = update.message.chat.id
    try:
        name = context.args[0]
        time_str = context.args[1]
        duration_min = int(context.args[2])
        url = context.args[3]

        if chat_id not in scheduled_jobs or not any(job_name == name for job_name, _, _ in scheduled_jobs[chat_id]):
            await update.message.reply_text(f"No existing schedule found for '{name}'. Use /schedule to create a new schedule.")
            return

        # Cancel the existing job
        for job_name, job, notify_job in scheduled_jobs[chat_id]:
            if job_name == name:
                job.stop()
                notify_job.stop()

        # Reschedule the job
        now = datetime.now()
        schedule_time = datetime.strptime(time_str, "%I:%M%p").replace(year=now.year, month=now.month, day=now.day)
        if schedule_time < now:
            schedule_time += timedelta(days=1)

        duration_sec = duration_min * 60
        notify_time = schedule_time - timedelta(minutes=5)  # Notify 5 minutes before the recording

        # Log the rescheduled time in seconds since epoch
        rescheduled_timestamp = int(schedule_time.timestamp())
        logger.info(f"Rescheduled recording '{name}' at {schedule_time.strftime('%Y-%m-%d %H:%M:%S')} ({rescheduled_timestamp} seconds since epoch).")

        cron_expression = f'{schedule_time.minute} {schedule_time.hour} * * *'
        job = aiocron.crontab(cron_expression, func=record_scheduled_stream, args=(chat_id, name, url, duration_sec))
        notify_cron_expression = f'{notify_time.minute} {notify_time.hour} * * *'
        notify_job = aiocron.crontab(notify_cron_expression, func=notify_user, args=(chat_id, name))

        # Update the scheduled job
        scheduled_jobs[chat_id] = [(name, job, notify_job) if job_name == name else (job_name, old_job, old_notify_job) for job_name, old_job, old_notify_job in scheduled_jobs[chat_id]]
        
        await update.message.reply_text(f"Rescheduled recording '{name}' at {time_str} for {duration_min} minutes.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /reschedule <name> <HH:MMAM/PM> <duration in minutes> <url>")
    except Exception as e:
        logger.error(f"Error rescheduling recording: {e}")
        await update.message.reply_text("Failed to reschedule recording. Please try again.")

async def list_schedules(update: Update, context: CallbackContext):
    chat_id = update.message.chat.id
    if chat_id in scheduled_jobs and scheduled_jobs[chat_id]:
        schedule_list = "\n".join([f"- {name}" for name, _, _ in scheduled_jobs[chat_id]])
        await update.message.reply_text(f"Scheduled recordings:\n{schedule_list}")
    else:
        await update.message.reply_text("No scheduled recordings found.")

async def cancel_schedule(update: Update, context: CallbackContext):
    chat_id = update.message.chat.id
    try:
        name = context.args[0]
        if chat_id in scheduled_jobs:
            for job_name, job, notify_job in scheduled_jobs[chat_id]:
                if job_name == name:
                    job.stop()
                    notify_job.stop()
                    scheduled_jobs[chat_id] = [job for job in scheduled_jobs[chat_id] if job[0] != name]
                    await update.message.reply_text(f"Canceled scheduled recording '{name}'.")
                    return
        await update.message.reply_text(f"No schedule found with the name '{name}'.")
    except IndexError:
        await update.message.reply_text("Usage: /cancel_schedule <name>")
    except Exception as e:
        logger.error(f"Error canceling schedule: {e}")
        await update.message.reply_text("Failed to cancel schedule. Please try again.")

async def delete_files(update: Update, context: CallbackContext):
    try:
        for file in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.isfile(file_path):
                os.remove(file_path)
        await update.message.reply_text("All stored videos have been deleted.")
    except Exception as e:
        logger.error(f"Error deleting files: {e}")
        await update.message.reply_text("Failed to delete files. Please try again.")

async def status(update: Update, context: CallbackContext):
    try:
        # Get disk usage
        usage = psutil.disk_usage('/')
        total_gb = usage.total / (1024 ** 3)
        used_gb = usage.used / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)

        # Get CPU and memory usage
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        memory_used_gb = memory.used / (1024 ** 3)
        memory_total_gb = memory.total / (1024 ** 3)

        status_message = (
            f"Disk Usage: {used_gb:.2f} GB used / {total_gb:.2f} GB total ({free_gb:.2f} GB free)\n"
            f"CPU Usage: {cpu_percent}%\n"
            f"Memory Usage: {memory_used_gb:.2f} GB used / {memory_total_gb:.2f} GB total"
        )

        await update.message.reply_text(status_message)
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        await update.message.reply_text("Failed to retrieve system status. Please try again.")

async def ls(update: Update, context: CallbackContext):
    try:
        files = os.listdir(DOWNLOAD_DIR)
        if files:
            file_list = "\n".join(files)
            await update.message.reply_text(f"Files in directory:\n{file_list}")
        else:
            await update.message.reply_text("No files found in the download directory.")
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        await update.message.reply_text("Failed to list files. Please try again.")

async def info(update: Update, context: CallbackContext):
    try:
        filename = context.args[0]
        file_path = os.path.join(DOWNLOAD_DIR, filename)

        if not os.path.exists(file_path):
            await update.message.reply_text("File not found.")
            return

        file_size = os.path.getsize(file_path)
        last_modified = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%d %H:%M:%S')

        await update.message.reply_text(f"File: {filename}\nSize: {file_size} bytes\nLast Modified: {last_modified}")
    except IndexError:
        await update.message.reply_text("Usage: /info <filename>")
    except Exception as e:
        logger.error(f"Error getting file info: {e}")
        await update.message.reply_text("Failed to retrieve file info. Please try again.")

async def upload(update: Update, context: CallbackContext):
    chat_id = update.message.chat.id
    try:
        filename = context.args[0]
        file_path = os.path.join(DOWNLOAD_DIR, filename)

        if not os.path.exists(file_path):
            await update.message.reply_text("File not found.")
            return

        await update.message.reply_text("Uploading file to PlayerX...")
        await upload_and_send_file(chat_id, file_path, 0)
    except IndexError:
        await update.message.reply_text("Usage: /upload <filename>")
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        await update.message.reply_text("Failed to upload file. Please try again.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(bot_token).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('set_duration', set_duration))
    app.add_handler(CommandHandler('schedule', schedule_recording))
    app.add_handler(CommandHandler('reschedule', reschedule_recording))
    app.add_handler(CommandHandler('list_schedules', list_schedules))
    app.add_handler(CommandHandler('cancel_schedule', cancel_schedule))
    app.add_handler(CommandHandler('delete', delete_files))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(CommandHandler('ls', ls))
    app.add_handler(CommandHandler('info', info))
    app.add_handler(CommandHandler('upload', upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, record_stream))

    keep_alive()
    app.run_polling()
