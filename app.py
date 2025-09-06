import os
import json
import uuid
import time
import logging
from flask import Flask, session, request, render_template_string, jsonify, redirect
from flask_socketio import SocketIO, emit, join_room
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError
import asyncio
import threading
from threading import Lock
import eventlet
eventlet.monkey_patch()

# إعدادات التسجيل
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

SESSIONS_DIR = "sessions"
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

USERS = {}
USERS_LOCK = Lock()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# ===========================
# تحميل جميع الجلسات عند البدء
# ===========================
def load_all_sessions():
    with USERS_LOCK:
        for filename in os.listdir(SESSIONS_DIR):
            if filename.endswith('.json'):
                user_id = filename.split('.')[0]
                try:
                    settings = load_settings(user_id)
                    if settings and 'api_id' in settings and 'api_hash' in settings:
                        client = TelegramClient(
                            os.path.join(SESSIONS_DIR, f"{user_id}_session"),
                            int(settings['api_id']),
                            settings['api_hash']
                        )
                        USERS[user_id] = {
                            'client': client,
                            'settings': settings,
                            'thread': None,
                            'is_running': False,
                            'stats': {"sent": 0, "errors": 0},
                            'loop': None
                        }
                        
                        # إعادة الاتصال التلقائي إذا كان مفعل
                        if settings.get('auto_reconnect', False):
                            USERS[user_id]['is_running'] = True
                            thread = threading.Thread(target=monitoring_task, args=(user_id,))
                            thread.daemon = True
                            thread.start()
                            USERS[user_id]['thread'] = thread
                            logger.info(f"تم إعادة الاتصال التلقائي للمستخدم {user_id}")
                except Exception as e:
                    logger.error(f"فشل تحميل الجلسة للمستخدم {user_id}: {str(e)}")

# ===========================
# حفظ واسترجاع الإعدادات
# ===========================
def save_settings(user_id, settings):
    path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=4)

def load_settings(user_id):
    path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# ===========================
# إدارة جلسات Telegram
# ===========================
async def setup_telegram_client(user_id, phone, api_id, api_hash, password=None, code=None):
    try:
        client = TelegramClient(
            os.path.join(SESSIONS_DIR, f"{user_id}_session"),
            int(api_id),
            api_hash
        )
        
        await client.connect()
        
        if not await client.is_user_authorized():
            try:
                await client.send_code_request(phone)
                if code:
                    await client.sign_in(phone, code, password=password)
                else:
                    return {"status": "code_required"}
            except SessionPasswordNeededError:
                return {"status": "password_required"}
            except PhoneNumberInvalidError:
                return {"status": "error", "message": "رقم الهاتف غير صحيح"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        
        return {"status": "success", "client": client}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ===========================
# مهمة المراقبة لكل مستخدم
# ===========================
def monitoring_task(user_id):
    with USERS_LOCK:
        if user_id not in USERS:
            return
        user_data = USERS[user_id]
    
    client = user_data['client']
    settings = user_data['settings']
    max_retries = int(settings.get('max_retries', 5))
    retry_count = 0
    
    # إنشاء event loop جديد لهذا thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    user_data['loop'] = loop
    
    async def async_monitor():
        nonlocal retry_count
        
        # تعريف معالج الرسائل الجديدة
        @client.on(events.NewMessage)
        async def handler(event):
            if not user_data['is_running']:
                return
                
            msg = event.message.message
            chat_id = event.chat_id
            
            # التحقق من كلمات المراقبة
            watch_words = settings.get("watch_words", [])
            if watch_words:
                for word in watch_words:
                    if word.lower() in msg.lower():
                        try:
                            await client.send_message('me', f"🔔 رصدت كلمة: {word}\n\nفي المحادثة: {chat_id}\n\nالرسالة: {msg}")
                            socketio.emit('log_update', {"message": f"🔔 رصدت كلمة: {word} في المحادثة {chat_id}"}, room=user_id)
                        except Exception as e:
                            socketio.emit('log_update', {"message": f"❌ خطأ في إرسال التنبيه: {str(e)}"}, room=user_id)
            
            socketio.emit('log_update', {"message": f"📩 {chat_id}: {msg}"}, room=user_id)

        # بدء الجلسة مع إعادة المحاولة
        while user_data['is_running'] and retry_count < max_retries:
            try:
                await client.start()
                await client.send_message('me', "🚀 بدأ نظام المراقبة والتذكير الآلي")
                socketio.emit('connection_status', {"status": "connected"}, room=user_id)
                socketio.emit('log_update', {"message": "✅ تم تسجيل الدخول والبدء بالمراقبة"}, room=user_id)
                retry_count = 0  # إعادة تعيين عداد المحاولات عند النجاح
                break
            except Exception as e:
                retry_count += 1
                socketio.emit('log_update', {"message": f"❌ فشل تسجيل الدخول ({retry_count}/{max_retries}): {str(e)}"}, room=user_id)
                await asyncio.sleep(10 * retry_count)  # زيادة وقت الانتظار مع كل محاولة

        if retry_count >= max_retries:
            socketio.emit('connection_status', {"status": "disconnected"}, room=user_id)
            socketio.emit('log_update', {"message": "❌ توقف المراقبة بعد عدد كبير من المحاولات الفاشلة"}, room=user_id)
            user_data['is_running'] = False
            return

        # حلقة الإرسال التلقائي
        while user_data['is_running']:
            try:
                if settings.get("send_type") == "automatic" and user_data['is_running']:
                    groups = settings.get("groups", [])
                    message = settings.get("message", "")
                    
                    if groups and message:
                        for group in groups:
                            if not user_data['is_running']:
                                break
                                
                            try:
                                await client.send_message(group, message)
                                socketio.emit('log_update', {"message": f"🚀 أرسلت رسالة إلى {group}"}, room=user_id)
                                
                                # تحديث الإحصائيات
                                user_data['stats']['sent'] += 1
                                socketio.emit('stats_update', user_data['stats'], room=user_id)
                                
                            except Exception as e:
                                socketio.emit('log_update', {"message": f"❌ {group}: {str(e)}"}, room=user_id)
                                user_data['stats']['errors'] += 1
                                socketio.emit('stats_update', user_data['stats'], room=user_id)
                
                # الانتظار للفترة الزمنية المحددة
                interval = int(settings.get("interval_seconds", 3600))
                for i in range(interval):
                    if not user_data['is_running']:
                        break
                    await asyncio.sleep(1)
                    
            except Exception as e:
                socketio.emit('log_update', {"message": f"❌ خطأ في حلقة الإرسال: {str(e)}"}, room=user_id)
                await asyncio.sleep(10)

    try:
        loop.run_until_complete(async_monitor())
    except Exception as e:
        socketio.emit('log_update', {"message": f"❌ توقف المراقبة بسبب خطأ: {str(e)}"}, room=user_id)
    finally:
        user_data['is_running'] = False
        socketio.emit('connection_status', {"status": "disconnected"}, room=user_id)
        loop.close()

# ===========================
# الراوتات
# ===========================
@app.route("/")
def index():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
        session.permanent = True
        
    user_id = session['user_id']
    settings = load_settings(user_id)
    connection_status = "disconnected"
    
    if user_id in USERS and USERS[user_id].get('is_running', False):
        connection_status = "connected"
    
    return render_template_string(INDEX_HTML, user_id=user_id, settings=settings, connection_status=connection_status)

@app.route("/admin")
def admin_dashboard():
    if not session.get('is_admin'):
        return redirect("/admin/login")
        
    users_data = []
    with USERS_LOCK:
        for user_id, user_info in USERS.items():
            users_data.append({
                'user_id': user_id,
                'is_running': user_info['is_running'],
                'stats': user_info.get('stats', {}),
                'phone': user_info['settings'].get('phone', '')
            })
    
    return render_template_string(ADMIN_HTML, users=users_data)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template_string(ADMIN_LOGIN_HTML)
    
    if request.form.get('password') == ADMIN_PASSWORD:
        session['is_admin'] = True
        return redirect('/admin')
    
    return render_template_string(ADMIN_LOGIN_HTML, error="كلمة المرور غير صحيحة")

@app.route("/api/save_login", methods=["POST"])
def api_save_login():
    user_id = session['user_id']
    data = request.json
    
    settings = {
        'phone': data.get('phone'),
        'api_id': data.get('api_id'),
        'api_hash': data.get('api_hash'),
        'password': data.get('password'),
        'login_time': time.time()
    }
    
    save_settings(user_id, settings)
    
    try:
        # استخدام eventlet لتنفيذ async functions
        with eventlet.Timeout(30):  # timeout after 30 seconds
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(setup_telegram_client(
                user_id, 
                settings['phone'], 
                settings['api_id'], 
                settings['api_hash'],
                settings.get('password')
            ))
        
        if result["status"] == "success":
            client = result["client"]
            with USERS_LOCK:
                USERS[user_id] = {
                    'client': client,
                    'settings': settings,
                    'thread': None,
                    'is_running': False,
                    'stats': {"sent": 0, "errors": 0},
                    'loop': None
                }
            return {"success": True, "message": "✅ تم حفظ البيانات وتسجيل الدخول", "code_required": False}
        elif result["status"] == "code_required":
            return {"success": True, "message": "📱 يرجى إدخال الكود المرسل", "code_required": True}
        elif result["status"] == "password_required":
            return {"success": True, "message": "🔒 يرجى إدخال كلمة المرور", "password_required": True}
        else:
            return {"success": False, "message": f"❌ خطأ: {result.get('message', 'Unknown error')}"}
            
    except Exception as e:
        return {"success": False, "message": f"❌ خطأ: {str(e)}"}

@app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    user_id = session['user_id']
    data = request.json
    code = data.get('code')
    
    settings = load_settings(user_id)
    
    if not settings:
        return {"success": False, "message": "❌ لم يتم حفظ بيانات الدخول بعد"}
    
    try:
        with eventlet.Timeout(30):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(setup_telegram_client(
                user_id, 
                settings['phone'], 
                settings['api_id'], 
                settings['api_hash'],
                settings.get('password'),
                code
            ))
        
        if result["status"] == "success":
            client = result["client"]
            with USERS_LOCK:
                USERS[user_id] = {
                    'client': client,
                    'settings': settings,
                    'thread': None,
                    'is_running': False,
                    'stats': {"sent": 0, "errors": 0},
                    'loop': None
                }
            return {"success": True, "message": "✅ تم التحقق من الكود بنجاح"}
        else:
            return {"success": False, "message": f"❌ فشل التحقق: {result.get('message', 'Unknown error')}"}
            
    except Exception as e:
        return {"success": False, "message": f"❌ خطأ: {str(e)}"}

@app.route("/api/save_settings", methods=["POST"])
def api_save_settings():
    user_id = session['user_id']
    data = request.json
    
    # تحميل الإعدادات الحالية
    current_settings = load_settings(user_id)
    
    # تحديث الإعدادات الجديدة
    current_settings.update({
        'message': data.get('message'),
        'groups': [g.strip() for g in data.get('groups', '').split('\n') if g.strip()],
        'interval_seconds': int(data.get('interval_seconds', 3600)),
        'watch_words': [w.strip() for w in data.get('watch_words', '').split('\n') if w.strip()],
        'send_type': data.get('send_type', 'manual'),
        'max_retries': int(data.get('max_retries', 5)),
        'auto_reconnect': data.get('auto_reconnect', False)
    })
    
    save_settings(user_id, current_settings)
    
    # تحديث الإعدادات في الذاكرة إذا كان المستخدم نشط
    with USERS_LOCK:
        if user_id in USERS:
            USERS[user_id]['settings'] = current_settings
    
    return {"success": True, "message": "✅ تم حفظ الإعدادات بنجاح"}

@app.route("/api/start_monitoring", methods=["POST"])
def api_start_monitoring():
    user_id = session['user_id']
    
    with USERS_LOCK:
        if user_id not in USERS:
            return {"success": False, "message": "❌ لم يتم إعداد الحساب بعد"}
        
        user_data = USERS[user_id]
        
        if user_data['is_running']:
            return {"success": False, "message": "✅ النظام يعمل بالفعل"}
        
        user_data['is_running'] = True
    
    # بدء thread المراقبة
    thread = threading.Thread(target=monitoring_task, args=(user_id,))
    thread.daemon = True
    thread.start()
    
    with USERS_LOCK:
        USERS[user_id]['thread'] = thread
    
    return {"success": True, "message": "🚀 بدأت المراقبة والإرسال التلقائي"}

@app.route("/api/stop_monitoring", methods=["POST"])
def api_stop_monitoring():
    user_id = session['user_id']
    
    with USERS_LOCK:
        if user_id in USERS:
            USERS[user_id]['is_running'] = False
            return {"success": True, "message": "⏹ توقفت المراقبة والإرسال التلقائي"}
    
    return {"success": False, "message": "❌ لم يتم تشغيل النظام"}

@app.route("/api/send_now", methods=["POST"])
def api_send_now():
    user_id = session['user_id']
    
    with USERS_LOCK:
        if user_id not in USERS:
            return {"success": False, "message": "❌ لم يتم إعداد الحساب بعد"}
        
        user_data = USERS[user_id]
        settings = user_data['settings']
        client = user_data['client']
    
    async def send_all():
        try:
            groups = settings.get("groups", [])
            message = settings.get("message", "")
            
            if not groups or not message:
                socketio.emit('log_update', {"message": "❌ لم يتم تحديد مجموعات أو رسالة للإرسال"}, room=user_id)
                return
            
            for group in groups:
                try:
                    await client.send_message(group, message)
                    socketio.emit('log_update', {"message": f"🚀 أرسلت رسالة إلى {group}"}, room=user_id)
                    
                    # تحديث الإحصائيات
                    with USERS_LOCK:
                        if 'stats' not in USERS[user_id]:
                            USERS[user_id]['stats'] = {"sent": 0, "errors": 0}
                        USERS[user_id]['stats']['sent'] += 1
                        socketio.emit('stats_update', USERS[user_id]['stats'], room=user_id)
                    
                except Exception as e:
                    socketio.emit('log_update', {"message": f"❌ {group}: {str(e)}"}, room=user_id)
                    
                    # تحديث الإحصائيات
                    with USERS_LOCK:
                        if 'stats' not in USERS[user_id]:
                            USERS[user_id]['stats'] = {"sent": 0, "errors": 0}
                        USERS[user_id]['stats']['errors'] += 1
                        socketio.emit('stats_update', USERS[user_id]['stats'], room=user_id)
                    
        except Exception as e:
            socketio.emit('log_update', {"message": f"❌ خطأ في الإرسال الفوري: {str(e)}"}, room=user_id)

    # تشغيل الإرسال في thread منفصل
    def run_send():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_all())
        loop.close()
    
    thread = threading.Thread(target=run_send)
    thread.daemon = True
    thread.start()
    
    return {"success": True, "message": "✅ بدأ الإرسال الفوري إلى جميع المجموعات"}

@app.route("/api/get_stats", methods=["GET"])
def api_get_stats():
    user_id = session['user_id']
    
    with USERS_LOCK:
        if user_id in USERS and 'stats' in USERS[user_id]:
            return jsonify(USERS[user_id]['stats'])
    
    return jsonify({"sent": 0, "errors": 0})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    user_id = session['user_id']
    
    with USERS_LOCK:
        if user_id in USERS:
            USERS[user_id]['is_running'] = False
            # إغلاق client Telegram
            try:
                if USERS[user_id]['loop']:
                    USERS[user_id]['loop'].close()
            except:
                pass
            
            # حذف من الذاكرة
            del USERS[user_id]
    
    # حذف ملف الإعدادات
    settings_path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
    if os.path.exists(settings_path):
        os.remove(settings_path)
    
    # حذف جلسة المستخدم
    session.clear()
    
    return {"success": True, "message": "✅ تم تسجيل الخروج بنجاح"}

# ===========================
# SocketIO
# ===========================
@socketio.on('join')
def on_join(data):
    user_id = session['user_id']
    join_room(user_id)
    with USERS_LOCK:
        if user_id in USERS and USERS[user_id].get('is_running', False):
            emit('connection_status', {"status": "connected"})
        else:
            emit('connection_status', {"status": "disconnected"})

# ===========================
# واجهات HTML
# ===========================
INDEX_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>نظام التليجرام الآلي - سرعة إنجاز</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        :root {
            --primary: #4e73df;
            --secondary: #6f42c1;
            --success: #1cc88a;
            --info: #36b9cc;
            --warning: #f6c23e;
            --danger: #e74a3b;
            --light: #f8f9fc;
            --dark: #5a5c69;
        }
        
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        body {
            background-color: #f8f9fc;
            color: #333;
            line-height: 1.6;
            padding: 0;
            margin: 0;
        }
        
        .header {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            color: white;
            padding: 15px 20px;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            position: relative;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .connection-status {
            position: absolute;
            top: 10px;
            left: 10px;
            background: var(--danger);
            color: white;
            padding: 5px 10px;
            border-radius: 20px;
            font-size: 12px;
            display: flex;
            align-items: center;
        }
        
        .connection-status.connected {
            background: var(--success);
        }
        
        .connection-status i {
            margin-right: 5px;
        }
        
        .container {
            max-width: 1200px;
            margin: 20px auto;
            padding: 0 20px;
        }
        
        .card {
            background: white;
            border-radius: 10px;
            box-shadow: 0 0 15px rgba(0, 0, 0, 0.1);
            margin-bottom: 20px;
            overflow: hidden;
        }
        
        .card-header {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            color: white;
            padding: 15px 20px;
            font-weight: bold;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .card-body {
            padding: 20px;
        }
        
        .form-group {
            margin-bottom: 15px;
        }
        
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
            color: var(--dark);
        }
        
        input, textarea, select {
            width: 100%;
            padding: 10px 15px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 16px;
            transition: border 0.3s;
        }
        
        input:focus, textarea:focus, select:focus {
            border-color: var(--primary);
            outline: none;
        }
        
        textarea {
            min-height: 100px;
            resize: vertical;
        }
        
        .btn {
            display: inline-block;
            padding: 10px 20px;
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 16px;
            transition: background 0.3s;
            text-align: center;
        }
        
        .btn:hover {
            background: var(--secondary);
        }
        
        .btn-success {
            background: var(--success);
        }
        
        .btn-success:hover {
            background: #17a673;
        }
        
        .btn-danger {
            background: var(--danger);
        }
        
        .btn-danger:hover {
            background: #d52a1a;
        }
        
        .btn-warning {
            background: var(--warning);
            color: #000;
        }
        
        .btn-warning:hover {
            background: #f4b619;
        }
        
        .flex-buttons {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }
        
        .flex-buttons .btn {
            flex: 1;
        }
        
        .icons-container {
            display: flex;
            justify-content: space-around;
            margin: 20px 0;
            flex-wrap: wrap;
        }
        
        .icon-box {
            text-align: center;
            padding: 20px;
            background: white;
            border-radius: 10px;
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
            margin: 10px;
            flex: 1;
            min-width: 250px;
        }
        
        .icon-box i {
            font-size: 40px;
            margin-bottom: 15px;
            color: var(--primary);
        }
        
        .log-container {
            background: #2e3440;
            color: #d8dee9;
            padding: 15px;
            border-radius: 5px;
            height: 300px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 14px;
        }
        
        .log-entry {
            margin-bottom: 5px;
            padding: 5px;
            border-bottom: 1px solid #4c566a;
        }
        
        .stats-container {
            display: flex;
            justify-content: space-around;
            text-align: center;
        }
        
        .stat-box {
            padding: 15px;
        }
        
        .stat-number {
            font-size: 24px;
            font-weight: bold;
            color: var(--primary);
        }
        
        .hidden {
            display: none;
        }
        
        .footer {
            text-align: center;
            padding: 20px;
            background: var(--dark);
            color: white;
            margin-top: 40px;
        }
        
        @media (max-width: 768px) {
            .flex-buttons {
                flex-direction: column;
            }
            
            .icons-container {
                flex-direction: column;
            }
            
            .stats-container {
                flex-direction: column;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="connection-status" id="connectionStatus">
            <i class="fas fa-times-circle"></i> غير متصل
        </div>
        <h1>نظام التليجرام الآلي</h1>
        <button class="btn btn-danger" onclick="logout()"><i class="fas fa-sign-out-alt"></i> خروج</button>
    </div>
    
    <div class="container">
        <!-- إعدادات الدخول -->
        <div class="card" id="loginSettings">
            <div class="card-header">
                <span><i class="fas fa-sign-in-alt"></i> إعدادات الدخول إلى Telegram</span>
            </div>
            <div class="card-body">
                <div class="form-group">
                    <label for="phone">رقم الهاتف:</label>
                    <input type="text" id="phone" placeholder="+1234567890">
                </div>
                <div class="form-group">
                    <label for="api_id">API ID:</label>
                    <input type="text" id="api_id">
                </div>
                <div class="form-group">
                    <label for="api_hash">API Hash:</label>
                    <input type="text" id="api_hash">
                </div>
                <div class="form-group">
                    <label for="password">كلمة المرور (اختياري):</label>
                    <input type="password" id="password">
                </div>
                <button class="btn" onclick="saveLogin()"><i class="fas fa-check"></i> موافق</button>
                
                <div id="codeSection" class="hidden">
                    <div class="form-group" style="margin-top: 20px;">
                        <label for="code">كود التحقق:</label>
                        <input type="text" id="code" placeholder="أدخل الكود المرسل إلى Telegram">
                    </div>
                    <button class="btn" onclick="verifyCode()"><i class="fas fa-check"></i> تأكيد الكود</button>
                </div>
            </div>
        </div>
        
        <!-- إعدادات التشغيل -->
        <div class="card hidden" id="operationSettings">
            <div class="card-header">
                <span><i class="fas fa-cog"></i> إعدادات التشغيل</span>
            </div>
            <div class="card-body">
                <div class="form-group">
                    <label for="message">الرسالة التي سترسل:</label>
                    <textarea id="message" placeholder="أدخل الرسالة التي تريد إرسالها إلى المجموعات"></textarea>
                </div>
                <div class="form-group">
                    <label for="groups">روابط المجموعات (كل رابط في سطر):</label>
                    <textarea id="groups" placeholder="https://t.me/group1&#10;https://t.me/group2"></textarea>
                </div>
                <div class="form-group">
                    <label for="interval_seconds">الوقت بين每一轮发送 (بالثواني):</label>
                    <input type="number" id="interval_seconds" value="3600">
                </div>
                <div class="form-group">
                    <label for="watch_words">كلمات المراقبة (كل كلمة في سطر):</label>
                    <textarea id="watch_words" placeholder="كلمة1&#10;كلمة2"></textarea>
                </div>
                <div class="form-group">
                    <label for="send_type">نوع الإرسال:</label>
                    <select id="send_type">
                        <option value="manual">يدوي</option>
                        <option value="automatic">تلقائي</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="max_retries">أقصى عدد من المحاولات الفاشلة:</label>
                    <input type="number" id="max_retries" value="5">
                </div>
                <div class="form-group">
                    <label for="auto_reconnect">إعادة الاتصال التلقائي عند بدء التشغيل:</label>
                    <select id="auto_reconnect">
                        <option value="false">لا</option>
                        <option value="true">نعم</option>
                    </select>
                </div>
                <button class="btn" onclick="saveSettings()"><i class="fas fa-save"></i> حفظ الإعدادات</button>
            </div>
        </div>
        
        <!-- أيقونات التحكم -->
        <div class="icons-container hidden" id="controlIcons">
            <div class="icon-box">
                <i class="fas fa-paper-plane"></i>
                <h3>الإرسال</h3>
                <p>إرسال الرسائل إلى المجموعات</p>
                <div class="flex-buttons">
                    <button class="btn btn-success" onclick="sendNow()"><i class="fas fa-bolt"></i> إرسال فوري</button>
                    <button class="btn" id="toggleAutoSend" onclick="toggleAutoSend()"><i class="fas fa-robot"></i> تشغيل تلقائي</button>
                </div>
            </div>
            
            <div class="icon-box">
                <i class="fas fa-binoculars"></i>
                <h3>المراقبة</h3>
                <p>مراقبة المجموعات للكلمات المحددة</p>
                <div class="flex-buttons">
                    <button class="btn btn-success" onclick="startMonitoring()"><i class="fas fa-play"></i> بدء المراقبة</button>
                    <button class="btn btn-danger" onclick="stopMonitoring()"><i class="fas fa-stop"></i> إيقاف المراقبة</button>
                </div>
            </div>
            
            <div class="icon-box">
                <i class="fas fa-chart-bar"></i>
                <h3>الإحصائيات</h3>
                <div class="stats-container">
                    <div class="stat-box">
                        <div class="stat-number" id="sentCount">0</div>
                        <div>الرسائل المرسلة</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-number" id="errorCount">0</div>
                        <div>الأخطاء</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- سجل الأحداث -->
        <div class="card">
            <div class="card-header">
                <span><i class="fas fa-history"></i> سجل الأحداث</span>
            </div>
            <div class="card-body">
                <div class="log-container" id="logContainer"></div>
            </div>
        </div>
    </div>
    
    <div class="footer">
        تصميم وتنفيذ أنور سيف، خصيصا لمركز سرعة انجاز 📚 للخدمات الطلابية والاكاديمية
    </div>

    <script>
        const socket = io();
        const userId = "{{ user_id }}";
        let isConnected = "{{ connection_status }}" === "connected";
        
        // انضمام إلى غرفة Socket.io
        socket.emit('join', {user_id: userId});
        
        // تحديث حالة الاتصال
        socket.on('connection_status', function(data) {
            isConnected = data.status === 'connected';
            updateConnectionStatus(isConnected);
        });
        
        // تحديث السجل
        socket.on('log_update', function(data) {
            addLogEntry(data.message);
        });
        
        // تحديث الإحصائيات
        socket.on('stats_update', function(data) {
            updateStats(data);
        });
        
        // تحديث حالة الاتصال في الواجهة
        function updateConnectionStatus(connected) {
            const statusElement = document.getElementById('connectionStatus');
            if (connected) {
                statusElement.innerHTML = '<i class="fas fa-check-circle"></i> متصل';
                statusElement.classList.add('connected');
                
                // إخفاء إعدادات الدخول وإظهار إعدادات التشغيل وأيقونات التحكم
                document.getElementById('loginSettings').classList.add('hidden');
                document.getElementById('operationSettings').classList.remove('hidden');
                document.getElementById('controlIcons').classList.remove('hidden');
            } else {
                statusElement.innerHTML = '<i class="fas fa-times-circle"></i> غير متصل';
                statusElement.classList.remove('connected');
            }
        }
        
        // إضافة مدخل إلى السجل
        function addLogEntry(message) {
            const logContainer = document.getElementById('logContainer');
            const logEntry = document.createElement('div');
            logEntry.className = 'log-entry';
            logEntry.textContent = `${new Date().toLocaleTimeString()} - ${message}`;
            logContainer.appendChild(logEntry);
            logContainer.scrollTop = logContainer.scrollHeight;
        }
        
        // تحديث الإحصائيات
        function updateStats(data) {
            document.getElementById('sentCount').textContent = data.sent || 0;
            document.getElementById('errorCount').textContent = data.errors || 0;
        }
        
        // حفظ بيانات الدخول
        function saveLogin() {
            const phone = document.getElementById('phone').value;
            const api_id = document.getElementById('api_id').value;
            const api_hash = document.getElementById('api_hash').value;
            const password = document.getElementById('password').value;
            
            fetch('/api/save_login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({phone, api_id, api_hash, password})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    addLogEntry(data.message);
                    if (data.code_required) {
                        document.getElementById('codeSection').classList.remove('hidden');
                    } else if (data.password_required) {
                        addLogEntry('يطلب كلمة المرور الثانية');
                    } else {
                        updateConnectionStatus(true);
                    }
                } else {
                    addLogEntry(data.message);
                }
            })
            .catch(error => {
                addLogEntry('خطأ في الاتصال: ' + error);
            });
        }
        
        // التحقق من الكود
        function verifyCode() {
            const code = document.getElementById('code').value;
            
            fetch('/api/verify_code', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({code})
            })
            .then(response => response.json())
            .then(data => {
                addLogEntry(data.message);
                if (data.success) {
                    document.getElementById('codeSection').classList.add('hidden');
                    updateConnectionStatus(true);
                }
            })
            .catch(error => {
                addLogEntry('خطأ في الاتصال: ' + error);
            });
        }
        
        // حفظ الإعدادات
        function saveSettings() {
            const message = document.getElementById('message').value;
            const groups = document.getElementById('groups').value;
            const interval_seconds = document.getElementById('interval_seconds').value;
            const watch_words = document.getElementById('watch_words').value;
            const send_type = document.getElementById('send_type').value;
            const max_retries = document.getElementById('max_retries').value;
            const auto_reconnect = document.getElementById('auto_reconnect').value;
            
            fetch('/api/save_settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({message, groups, interval_seconds, watch_words, send_type, max_retries, auto_reconnect})
            })
            .then(response => response.json())
            .then(data => {
                addLogEntry(data.message);
            })
            .catch(error => {
                addLogEntry('خطأ في الاتصال: ' + error);
            });
        }
        
        // بدء المراقبة
        function startMonitoring() {
            fetch('/api/start_monitoring', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                addLogEntry(data.message);
                document.getElementById('toggleAutoSend').classList.add('btn-success');
                document.getElementById('toggleAutoSend').innerHTML = '<i class="fas fa-robot"></i> إيقاف تلقائي';
            })
            .catch(error => {
                addLogEntry('خطأ في الاتصال: ' + error);
            });
        }
        
        // إيقاف المراقبة
        function stopMonitoring() {
            fetch('/api/stop_monitoring', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                addLogEntry(data.message);
                document.getElementById('toggleAutoSend').classList.remove('btn-success');
                document.getElementById('toggleAutoSend').innerHTML = '<i class="fas fa-robot"></i> تشغيل تلقائي';
            })
            .catch(error => {
                addLogEntry('خطأ في الاتصال: ' + error);
            });
        }
        
        // إرسال فوري
        function sendNow() {
            fetch('/api/send_now', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                addLogEntry(data.message);
            })
            .catch(error => {
                addLogEntry('خطأ في الاتصال: ' + error);
            });
        }
        
        // تبديل الإرسال التلقائي
        function toggleAutoSend() {
            const btn = document.getElementById('toggleAutoSend');
            if (btn.classList.contains('btn-success')) {
                stopMonitoring();
            } else {
                startMonitoring();
            }
        }
        
        // تسجيل الخروج
        function logout() {
            if (confirm('هل تريد تسجيل الخروج؟')) {
                fetch('/api/logout', {
                    method: 'POST'
                })
                .then(response => response.json())
                .then(data => {
                    addLogEntry(data.message);
                    setTimeout(() => {
                        window.location.reload();
                    }, 1000);
                })
                .catch(error => {
                    addLogEntry('خطأ في الاتصال: ' + error);
                });
            }
        }
        
        // تحميل الإحصائيات عند بدء التحميل
        window.onload = function() {
            fetch('/api/get_stats')
            .then(response => response.json())
            .then(data => {
                updateStats(data);
            });
            
            // تحديث حالة الاتصال الأولية
            updateConnectionStatus(isConnected);
            
            // إذا كان متصلاً، إخفاء إعدادات الدخول
            if (isConnected) {
                document.getElementById('loginSettings').classList.add('hidden');
                document.getElementById('operationSettings').classList.remove('hidden');
                document.getElementById('controlIcons').classList.remove('hidden');
            }
            
            // تحميل الإعدادات إذا كانت موجودة
            const settings = {{ settings | tojson }};
            if (settings.phone) document.getElementById('phone').value = settings.phone;
            if (settings.api_id) document.getElementById('api_id').value = settings.api_id;
            if (settings.api_hash) document.getElementById('api_hash').value = settings.api_hash;
            if (settings.message) document.getElementById('message').value = settings.message;
            if (settings.groups) document.getElementById('groups').value = settings.groups.join('\n');
            if (settings.interval_seconds) document.getElementById('interval_seconds').value = settings.interval_seconds;
            if (settings.watch_words) document.getElementById('watch_words').value = settings.watch_words.join('\n');
            if (settings.send_type) document.getElementById('send_type').value = settings.send_type;
            if (settings.max_retries) document.getElementById('max_retries').value = settings.max_retries;
            if (settings.auto_reconnect) document.getElementById('auto_reconnect').value = settings.auto_reconnect.toString();
        };
    </script>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>لوحة التحكم - المشرف</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #f8f9fc;
            margin: 0;
            padding: 0;
        }
        .header {
            background: linear-gradient(135deg, #4e73df 0%, #6f42c1 100%);
            color: white;
            padding: 15px 20px;
            text-align: center;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .container {
            max-width: 1200px;
            margin: 20px auto;
            padding: 0 20px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 0 15px rgba(0, 0, 0, 0.1);
        }
        th, td {
            padding: 12px 15px;
            text-align: right;
            border-bottom: 1px solid #ddd;
        }
        th {
            background-color: #4e73df;
            color: white;
        }
        tr:hover {
            background-color: #f5f5f5;
        }
        .btn {
            padding: 8px 15px;
            background: #4e73df;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>لوحة تحكم المشرف</h1>
        <a href="/" class="btn">العودة للتطبيق</a>
    </div>
    
    <div class="container">
        <h2>المستخدمون النشطون</h2>
        <table>
            <tr>
                <th>رقم الهاتف</th>
                <th>الحالة</th>
                <th>الرسائل المرسلة</th>
                <th>الأخطاء</th>
                <th>الإجراءات</th>
            </tr>
            {% for user in users %}
            <tr>
                <td>{{ user.phone }}</td>
                <td>{{ "نشط" if user.is_running else "غير نشط" }}</td>
                <td>{{ user.stats.sent }}</td>
                <td>{{ user.stats.errors }}</td>
                <td>
                    <button class="btn">تفاصيل</button>
                </td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""

ADMIN_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تسجيل الدخول - المشرف</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #f8f9fc;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .login-container {
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 0 15px rgba(0, 0, 0, 0.1);
            width: 300px;
            text-align: center;
        }
        h2 {
            color: #4e73df;
            margin-bottom: 20px;
        }
        input {
            width: 100%;
            padding: 10px;
            margin: 10px 0;
            border: 1px solid #ddd;
            border-radius: 5px;
        }
        button {
            width: 100%;
            padding: 10px;
            background: #4e73df;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
        }
        .error {
            color: #e74a3b;
            margin-bottom: 10px;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h2>تسجيل الدخول كمسؤول</h2>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="POST">
            <input type="password" name="password" placeholder="كلمة مرور المشرف" required>
            <button type="submit">دخول</button>
        </form>
    </div>
</body>
</html>
"""

# ===========================
# تشغيل السيرفر
# ===========================
if __name__ == "__main__":
    load_all_sessions()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
