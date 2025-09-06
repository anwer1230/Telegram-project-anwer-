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
USERS_LOCK = Lock()  # لمنع حالة تنافسية عند الوصول إلى USERS
ADMIN_PASSWORD = "admin123"  # يجب تغيير هذا في بيئة الإنتاج

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
        import eventlet
        eventlet.monkey_patch()
        
        with eventlet.Timeout(30):  # timeout after 30 seconds
            result = eventlet.import_patched('app').setup_telegram_client(
                user_id, 
                settings['phone'], 
                settings['api_id'], 
                settings['api_hash'],
                settings.get('password')
            )
        
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
        return {"success": False, "message": f"❌ خطأ: {str(e)}"})

@app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    user_id = session['user_id']
    data = request.json
    code = data.get('code')
    
    settings = load_settings(user_id)
    
    if not settings:
        return {"success": False, "message": "❌ لم يتم حفظ بيانات الدخول بعد"}
    
    try:
        import eventlet
        eventlet.monkey_patch()
        
        with eventlet.Timeout(30):
            result = eventlet.import_patched('app').setup_telegram_client(
                user_id, 
                settings['phone'], 
                settings['api_id'], 
                settings['api_hash'],
                settings.get('password'),
                code
            )
        
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
        return {"success": False, "message": f"❌ خطأ: {str(e)}"})

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
        /* الأنماط كما في الكود السابق */
    </style>
</head>
<body>
    <!-- المحتوى كما في الكود السابق -->
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
        /* أنماط خاصة بلوحة المشرف */
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
        /* أنماط صفحة تسجيل الدخول */
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
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
