import requests
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta, timezone
import time
from telegram import Update, ParseMode
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
)
import threading

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
iam_token = ""
OAUTH_TOKEN = os.getenv('OAUTH_TOKEN')

API_URL = 'https://compute.api.cloud.yandex.net/compute/v1/instances'

def api_request_get(url):
    header = {
        'Authorization': f'Bearer {iam_token}'
    }
    response = requests.get(url, headers=header)

    return response

def get_iam_token(oauth_token):
    url = 'https://iam.api.cloud.yandex.net/iam/v1/tokens'
    headers = {
        'Content-Type': 'application/json'
    }
    data = {
        'yandexPassportOauthToken': oauth_token
    }
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    token_data = response.json()
    return token_data['iamToken'], token_data['expiresAt']

token_is_ready = threading.Event()
def iam_token_updater(oauth_token):
    global iam_token
    while True:
        iam_token, expires_at = get_iam_token(oauth_token)

        token_is_ready.set()

        expires_at_datetime = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))

        time_to_expire = (expires_at_datetime - datetime.now(timezone.utc)).total_seconds()
        time.sleep(max(0, time_to_expire - 3600))

def check_dates(instances):
    date_now = datetime.now()
    date_now = date_now.strftime("%d.%m.%Y")
    current_date_obj = datetime.strptime(date_now, "%d.%m.%Y")

    res = []
    for i in instances:
        if i['Status'] == 'RUNNING':
            expire_date = datetime.strptime(i['expired_date'], "%d.%m.%Y")
            if current_date_obj >= expire_date:
                res.append(i)
    
    return res

def get_clouds():
    err = ""
    res = []
    response = api_request_get('https://resource-manager.api.cloud.yandex.net/resource-manager/v1/clouds')

    if response.status_code == 200:
        clouds = response.json()['clouds']
        for i in range(len(clouds)):
            res.append({'ID': clouds[i]['id'], 'Name': clouds[i]['name'], 'Folders': []})
    else:
        err = "error: Cloud connection"
        return err, response
    
    return err, res

def get_folders(cloud_id):
    err = ""
    res = []
    response = api_request_get(f'https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders?cloudId={cloud_id}')

    if response.status_code == 200:
        folders = response.json()['folders']
        for i in range(len(folders)):
            res.append({'ID': folders[i]['id'], 'Name': folders[i]['name'], 'Instances': []})
    else:
        err = "error: Folder connection"
        return err, response
    
    return err, res

def get_instances(folder_id):
    err = ""
    res = []
    response = api_request_get(f'{API_URL}?folderId={folder_id}')

    if response.status_code == 200:
        instances = response.json()['instances']
        for i in instances:
            res.append({'ID': i['id'], 'Name': i['name'], 'Status': i['status'], 'expired_date': i['labels']['expired_date']})
    else:
        err = "error: Instance connection"
        return err, response
    
    return err, res

def get_instances_full():
    res = []

    err, clouds = get_clouds()
    if err != "":
        return err
    
    res.extend(clouds)

    for i in range(len(res)):
        err, folders = get_folders(res[i]["ID"])
        if err != "":
            return err
        
        res[i]["Folders"].extend(folders)

        for j in range(len(res[i]["Folders"])):
            err, instances = get_instances(res[i]["Folders"][j]["ID"])
            if err != "":
                return err
            
            res[i]["Folders"][j]["Instances"].extend(instances)

    return res

def stop_instances(instances):
    return_ = ""
    for i in instances:
        instance_id = i["ID"]
        header = {
            'Authorization': f'Bearer {iam_token}'
        }
        response = requests.post(f'{API_URL}/{instance_id}:stop', headers=header)
        
        if response.status_code == 200:
            return_ += f"Виртуальная машина {instance_id} завершает работу \n"
        else:
            return_ += f"Error {instance_id}: {response.status_code}, {response.text} \n"

    return return_

class Bot:
    def __init__(self):
        self.updater = Updater(TOKEN, use_context=True)
        self.dp = self.updater.dispatcher

        self.connection = False

        self.auto_shutdown = False
        self.auto_shutdown_thread = None
        self.stop_auto_shutdown_thread = threading.Event()

        self.auto_shutdown_start_time = "00:01"
        self.auto_shutdown_period_time = "24:00"

        self.full_instances = []
        self.instances = []

        self.commands_handler()

    def command_show_help(self, update: Update, context):
        update.message.reply_text(
            '/info - выводит информацию об организации \n' +
            '/instances - выводит информацию обо всех имеющихся виртуальных машинах \n\n' +
            '/check_expired - выводит информацию обо всех машинах, которые должны быть отключены, и дает возможность их отключить \n' +
            '/auto_shutdown - дает возможность влючать/отключать автоматическое отключение машин \n'
            )

    def command_update_instances(self):
        self.full_instances = get_instances_full()
        self.instances = []
        for i in self.full_instances:
            for j in i['Folders']:
                for k in j['Instances']:
                    self.instances.append(k)

    def command_info(self, update: Update, context):
        msg = update.message.reply_text("🔄 Ожидайте...")
        response = api_request_get('https://organization-manager.api.cloud.yandex.net/organization-manager/v1/organizations')

        if response.status_code == 200:
            self.connection = True

            org = response.json()['organizations']
            org_id = org[0]['id']
            org_name = org[0]['title']
        
        else:
            self.connection = False
            msg.edit_text("❌ Организация не подключена!")
            return

        message = 'Общая информация \n' + \
            f'*Текущее время:* {datetime.now()} \n' + \
            f'*Организация:* \n' + \
            f'│  *Имя:* {org_name} \n' + \
            f'│  *ID:* {org_id} \n\n'
        if self.auto_shutdown:
            message += \
                '✔️ Автоматическое отключение *включено*'
        else:
            message += \
                '❌ Автоматическое отключение *выключено*'

        msg.edit_text(message, parse_mode=ParseMode.MARKDOWN)

    def command_output_full_instances(self, update: Update, context):
        msg = update.message.reply_text("🔄 Ожидайте...")
        self.command_update_instances()
        res = ''

        for i in self.full_instances:
            res += '── ' + i['Name'] + '\n'

            for j in i['Folders']:
                res += '   └─ ' + j['Name'] + '\n'

                for k in j['Instances']:
                    res += '      └─ ' + k['Name'] + '\n' + \
                            '         ID: ' + k['ID'] + '\n' + \
                            '         Status: ' + k['Status'] + '\n' + \
                            '         expired_date: ' + k['expired_date'] + '\n'

        msg.edit_text("Полный список всех виртуальных машин" + "```\n"+ res +'```', parse_mode=ParseMode.MARKDOWN)

    def command_check_expired(self, update: Update, context):
        msg = update.message.reply_text("🔄 Ожидайте...")
        self.command_update_instances()
        expired = check_dates(self.instances)

        if len(expired) == 0:
            msg.edit_text("Машины, которые должны быть отключены не найдены")
            return ConversationHandler.END

        context.user_data['expired'] = expired

        message = '```\n'
        for i in expired:
            message += '└─ ' + 'Name: ' + i['Name'] + '\n   ID: ' + i['ID'] + '\n   expired_date: ' + i['expired_date'] + '\n'
        message += '```'

        msg.edit_text('Машины которые должны быть выключены: \n' + message + '\nВыключить данные машины (Да/...)', parse_mode=ParseMode.MARKDOWN)
        return "shutdown"

    def command_shutdown_instances(self, update: Update, context):
        if update.message.text == "Да":
            res = stop_instances(context.user_data['expired'])
            update.message.reply_text(res)

            self.command_update_instances()
            return ConversationHandler.END
        else:
            return ConversationHandler.END
        
    def command_auto_shutdown(self, update: Update, context):
        msg = f'*Время запуска:* {self.auto_shutdown_start_time}\n' + \
            f'*Перидо запуска:* {self.auto_shutdown_period_time}\n\n' + \
            'Изменить времени запуска и периода запуска? (Изменить/...)'
            
        if self.auto_shutdown:
            update.message.reply_text("*Выключить* автоматическое отключение машин? (Да/...) \n\n" + msg, parse_mode=ParseMode.MARKDOWN)
        else:
            update.message.reply_text("*Включить* автоматическое отключение машин? (Да/...) \n\n" + msg, parse_mode=ParseMode.MARKDOWN)

        return "edit"

    def next_run(self, start_time, period):
        dt_start = datetime.strptime(start_time, "%H:%M")

        if period == "24:00":
            minutes = 1440
        elif period == "00:00":
            minutes = 1
        else:
            dt_period = datetime.strptime(period, "%H:%M")
            minutes = dt_period.hour * 60 + dt_period.minute
        
        now = datetime.now()
        
        start_today = now.replace(hour=dt_start.hour, minute=dt_start.minute, second=0, microsecond=0)
        
        if now < start_today:
            next_run = start_today
        else:
            time_since_start = (now - start_today).total_seconds() // 60
            periods_passed = time_since_start // minutes
            next_run_minutes = (periods_passed + 1) * minutes
            next_run = start_today + timedelta(minutes=next_run_minutes)
        
        seconds_to_next_run = (next_run - now).total_seconds()
        return int(seconds_to_next_run)

    def auto_shutdown_worker(self, update: Update):
        while not self.stop_auto_shutdown_thread.is_set():
            self.command_update_instances()

            to_stop = stop_instances(check_dates(self.instances))
            if to_stop != "":
                update.message.reply_text(to_stop)

            run = self.next_run(self.auto_shutdown_start_time, self.auto_shutdown_period_time)
            
            if self.stop_auto_shutdown_thread.wait(run):
                break

    def command_edit(self, update: Update, context):
        text = update.message.text
        if text == "Изменить":
            update.message.reply_text(
                f'*Время запуска:* {self.auto_shutdown_start_time}\n' + \
                f'*Перидо запуска:* {self.auto_shutdown_period_time}\n\n' + \
                'Для изменнеия времени запуска и периода запуска напишите две строки вида чч:мм: \n' + \
                '*Например:* \n\n00:01\n24:00\n', parse_mode=ParseMode.MARKDOWN
                )
            return "edit_wait"
        
        elif text == "Да":
            if self.auto_shutdown:
                self.auto_shutdown = False

                self.stop_auto_shutdown_thread.set()
                if self.auto_shutdown_thread is not None:
                    self.auto_shutdown_thread.join() 

                    update.message.reply_text("Автоматическое отключение ❌*выключено*", parse_mode=ParseMode.MARKDOWN)

                return ConversationHandler.END
            
            else:
                self.auto_shutdown = True
                if self.auto_shutdown_thread is None or not self.auto_shutdown_thread.is_alive():
                    self.stop_auto_shutdown_thread.clear()

                    self.auto_shutdown_thread = threading.Thread(target=self.auto_shutdown_worker, args=(update,))
                    self.auto_shutdown_thread.start()
                    
                    update.message.reply_text("Автоматическое отключение ✔️*включено*", parse_mode=ParseMode.MARKDOWN)

                return ConversationHandler.END
        else:
            update.message.reply_text("Отменено")
            return ConversationHandler.END

    def command_edit_wait(self, update: Update, context):
        text = update.message.text.split("\n")
        
        try:
            datetime.strptime(text[0], "%H:%M")
            self.auto_shutdown_start_time = text[0]

            if text[1] != "24:00":
                datetime.strptime(text[1], "%H:%M")
            self.auto_shutdown_period_time = text[1]

            update.message.reply_text(
                'Изменено на: \n' + \
                f'*Время запуска:* {self.auto_shutdown_start_time}\n' + \
                f'*Перидо запуска:* {self.auto_shutdown_period_time}\n\n'
                , parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END
        
        except:
            update.message.reply_text("Отменено")
            return ConversationHandler.END

    def commands_handler(self):
        self.dp.add_handler(CommandHandler("info", self.command_info))
        self.dp.add_handler(CommandHandler("instances", self.command_output_full_instances))

        conv_handler_check_expired = ConversationHandler(
            entry_points=[CommandHandler('check_expired', self.command_check_expired)],
            states={
                'shutdown': [MessageHandler(Filters.text & ~Filters.command, self.command_shutdown_instances)],
            },
            fallbacks=[]
        )
        conv_handler_auto_shutdown = ConversationHandler(
            entry_points=[CommandHandler('auto_shutdown', self.command_auto_shutdown)],
            states={
                'edit': [MessageHandler(Filters.text & ~Filters.command, self.command_edit)],
                'edit_wait': [MessageHandler(Filters.text & ~Filters.command, self.command_edit_wait)],
            },
            fallbacks=[]
        )

        self.dp.add_handler(conv_handler_check_expired)
        self.dp.add_handler(conv_handler_auto_shutdown)

        self.dp.add_handler(MessageHandler(Filters.text & ~Filters.command, self.command_show_help))
    
    def start(self):
        self.updater.start_polling()
        self.updater.idle()


if __name__ == '__main__':
    bot = Bot()

    token_updater_thread = threading.Thread(target=iam_token_updater, args=(OAUTH_TOKEN,))
    token_updater_thread.start()

    token_is_ready.wait()

    bot.start()