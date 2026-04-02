import logging
import re
import m3u8
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
import requests
from urllib.parse import urlparse

# تفعيل التسجيل للأخطاء
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ===== إعدادات البوت =====
BOT_TOKEN = "8740155258:AAFIxKUoFlTIFvxqUHzsKSY8cGsUT_74DhA"
MAX_CHECKS_PER_DAY = 5

# قاموس لتخزين عدد الفحوصات لكل مستخدم
user_checks = {}

def get_user_checks_today(user_id):
    today = datetime.now().date()
    if user_id not in user_checks:
        user_checks[user_id] = {'date': today, 'count': 0}
    if user_checks[user_id]['date'] != today:
        user_checks[user_id] = {'date': today, 'count': 0}
    return user_checks[user_id]['count']

def increment_user_checks(user_id):
    user_checks[user_id]['count'] += 1

def is_iptv_link(text):
    """التعرف على روابط IPTV"""
    iptv_patterns = [
        r'\.m3u8',           # روابط HLS
        r'\.ts',             # مقاطع الفيديو
        r'get\.php\?',       # روابط Xtream Codes
        r'username=.+&password=',  # روابط تحتوي على معلومات دخول
        r'rtmp://',          # روابط RTMP
        r'udp://',           # روابط UDP
        r'playlist\.m3u',    # قوائم التشغيل
    ]
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in iptv_patterns)

def check_iptv_stream(url):
    """فحص روابط IPTV بشكل متقدم"""
    results = {
        'status': 'unknown',
        'type': 'unknown',
        'details': []
    }
    
    # إضافة headers محاكاة لمشغل فيديو حقيقي
    headers = {
        'User-Agent': 'VLC/3.0.18 LibVLC/3.0.18',
        'Accept': '*/*',
        'Connection': 'keep-alive'
    }
    
    try:
        # روابط Xtream Codes (مع username/password)
        if 'get.php' in url and 'username=' in url:
            results['type'] = 'Xtream Codes API'
            # استخراج معلومات الاتصال
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            
            if 'username' in params and 'password' in params:
                base_url = f"{parsed.scheme}://{parsed.netloc}"
                username = params['username'][0]
                
                # تجربة جلب قائمة القنوات
                player_api = f"{base_url}/player_api.php?username={username}&password={params['password'][0]}"
                try:
                    resp = requests.get(player_api, headers=headers, timeout=15)
                    if resp.status_code == 200:
                        data = resp.json()
                        if 'user_info' in data:
                            exp_date = data['user_info'].get('exp_date', 0)
                            if int(exp_date) > 0:
                                from datetime import datetime
                                exp_datetime = datetime.fromtimestamp(int(exp_date))
                                if exp_datetime > datetime.now():
                                    results['status'] = 'active'
                                    results['details'].append(f"✅ صلاحية الحساب: حتى {exp_datetime.strftime('%Y-%m-%d')}")
                                else:
                                    results['status'] = 'expired'
                                    results['details'].append("❌ الحساب منتهي الصلاحية")
                            if 'server_info' in data:
                                results['details'].append(f"🖥️ السيرفر: {data['server_info'].get('server', 'غير معروف')}")
                except:
                    pass
        
        # روابط M3U8
        elif '.m3u8' in url.lower():
            results['type'] = 'HLS Stream (.m3u8)'
            try:
                # محاولة تحليل ملف M3U8
                playlist = m3u8.load(url, headers=headers, timeout=20)
                if playlist.segments and len(playlist.segments) > 0:
                    results['status'] = 'active'
                    duration_sum = sum(segment.duration for segment in playlist.segments if segment.duration)
                    results['details'].append(f"✅ رابط صالح - {len(playlist.segments)} مقطع فيديو")
                    if playlist.target_duration:
                        results['details'].append(f"⏱️ مدة كل مقطع: {playlist.target_duration} ثانية")
                else:
                    results['status'] = 'no_segments'
                    results['details'].append("⚠️ الرابط لا يحتوي على مقاطع فيديو")
            except Exception as e:
                # محاولة فحص بسيط للرابط
                resp = requests.get(url, headers=headers, timeout=15, stream=True)
                if resp.status_code == 200:
                    content = resp.text[:500]
                    if '#EXTM3U' in content or '#EXTINF' in content:
                        results['status'] = 'active'
                        results['details'].append("✅ ملف M3U8 صالح")
                    else:
                        results['status'] = 'maybe'
                        results['details'].append("⚠️ الرابط يستجيب لكن ليس بصيغة M3U8 واضحة")
                else:
                    results['status'] = 'inactive'
                    results['details'].append(f"❌ رمز الخطأ: {resp.status_code}")
        
        # الروابط العادية
        else:
            results['type'] = 'Standard HTTP'
            try:
                resp = requests.get(url, headers=headers, timeout=10, stream=True)
                content_type = resp.headers.get('Content-Type', '')
                
                if resp.status_code == 200:
                    if 'video' in content_type:
                        results['status'] = 'active'
                        results['details'].append("✅ فيديو مباشر - يبدو شغالاً")
                    else:
                        # جلب أول 500 حرف للتحليل
                        chunk = next(resp.iter_content(500), b'')
                        if b'#EXTM3U' in chunk or b'#EXTINF' in chunk:
                            results['status'] = 'active'
                            results['details'].append("✅ رابط M3U صالح")
                        else:
                            results['status'] = 'maybe'
                            results['details'].append("⚠️ الرابط يستجيب لكن نوع المحتوى غير واضح")
                else:
                    results['status'] = 'inactive'
                    results['details'].append(f"❌ رمز الخطأ: {resp.status_code}")
            except requests.exceptions.Timeout:
                results['status'] = 'timeout'
                results['details'].append("⏰ الرابط لا يستجيب (انتهى الوقت)")
            except Exception as e:
                results['status'] = 'error'
                results['details'].append(f"⚠️ خطأ: {str(e)[:100]}")
                
    except Exception as e:
        results['status'] = 'error'
        results['details'].append(f"⚠️ خطأ عام: {str(e)[:100]}")
    
    return results

async def start(update: Update, context):
    welcome_message = (
        "🎯 *مرحباً بك في بوت فحص IPTV!*\n\n"
        "📌 *يمكنك إرسال:*\n"
        "• رابط `.m3u8` أو `.m3u`\n"
        "• رابط Xtream Codes (get.php?username=X&password=Y)\n"
        "• رابط عادي `http://...`\n\n"
        f"⚠️ *الحد الأقصى:* {MAX_CHECKS_PER_DAY} فحص/يوم\n"
        "💡 أرسل /help للمساعدة"
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def help_command(update: Update, context):
    help_text = (
        "🔍 *كيفية استخدام البوت لفحص IPTV:*\n\n"
        "*أنواع الروابط المدعومة:*\n"
        "• `http://server.com/channel.m3u8` - رابط HLS\n"
        "• `http://server.com:8080/get.php?username=user&password=pass` - رابط Xtream\n"
        "• `http://server.com:8080/playlist.m3u` - قائمة تشغيل\n\n"
        "*معلومات ستحصل عليها:*\n"
        "• صلاحية الحساب (لروابط Xtream)\n"
        "• عدد مقاطع الفيديو في الرابط\n"
        "• حالة البث (شغال/غير شغال)\n\n"
        f"📈 *الحد اليومي:* {MAX_CHECKS_PER_DAY} فحص\n"
        "🔄 يتجدد العداد كل يوم"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def status_command(update: Update, context):
    user_id = update.effective_user.id
    checks_today = get_user_checks_today(user_id)
    remaining = MAX_CHECKS_PER_DAY - checks_today
    await update.message.reply_text(
        f"📊 *حساب الفحوصات اليومية:*\n\n"
        f"✅ استخدمت: {checks_today}\n"
        f"📈 متبقي: {remaining}\n"
        f"🔢 الحد الأقصى: {MAX_CHECKS_PER_DAY}\n\n"
        f"🔄 يتجدد العداد عند منتصف الليل"
    , parse_mode='Markdown')

async def handle_link(update: Update, context):
    user_id = update.effective_user.id
    link_text = update.message.text.strip()
    
    checks_today = get_user_checks_today(user_id)
    if checks_today >= MAX_CHECKS_PER_DAY:
        await update.message.reply_text(f"❌ تجاوزت الحد اليومي ({MAX_CHECKS_PER_DAY} فحوصات). حاول غداً.")
        return
    
    processing_msg = await update.message.reply_text("⏳ *جارٍ فحص رابط IPTV...*\nقد يستغرق هذا 10-20 ثانية", parse_mode='Markdown')
    
    # تحديد نوع الرابط
    link_type = "IPTV" if is_iptv_link(link_text) else "عادي"
    
    # فحص الرابط
    result = check_iptv_stream(link_text)
    
    # بناء رسالة النتيجة
    result_text = f"🔍 *نتيجة فحص {'IPTV' if link_type == 'IPTV' else 'رابط'}:*\n\n"
    result_text += f"📎 `{link_text[:80]}{'...' if len(link_text) > 80 else ''}`\n\n"
    result_text += f"📡 *نوع الرابط:* {result['type']}\n"
    
    # حالة الرابط
    status_icons = {
        'active': '✅ شغال',
        'inactive': '❌ غير شغال',
        'timeout': '⏰ لا يستجيب',
        'expired': '📅 منتهي الصلاحية',
        'maybe': '⚠️ يحتاج اختبار',
        'error': '⚠️ خطأ في الفحص',
        'no_segments': '⚠️ لا يحتوي على فيديو'
    }
    result_text += f"📊 *الحالة:* {status_icons.get(result['status'], result['status'])}\n"
    
    # التفاصيل
    if result['details']:
        result_text += f"\n📋 *التفاصيل:*\n"
        for detail in result['details']:
            result_text += f"{detail}\n"
    
    increment_user_checks(user_id)
    remaining = MAX_CHECKS_PER_DAY - get_user_checks_today(user_id)
    result_text += f"\n---\n📊 *الفحوصات المتبقية اليوم:* {remaining}/{MAX_CHECKS_PER_DAY}"
    
    await processing_msg.edit_text(result_text, parse_mode='Markdown')

def main():
    print("🚀 *بوت LinkHunteroneBot يعمل الآن...*")
    print("📺 *يدعم فحص روابط IPTV (M3U8, Xtream Codes)*")
    print("✅ جاهز لاستقبال الطلبات!")
    
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()