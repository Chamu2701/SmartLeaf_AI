import os
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from transformers import pipeline, AutoImageProcessor, AutoModelForImageClassification
from PIL import Image
from groq import Groq
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'smartleafai_secret_key_987654321_botanical')

# Configure SQLite Database for Tracking Scans
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///garden.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
db = SQLAlchemy(app)

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize Hugging Face Image Classifier with Local Cache Fallback Check
# Initialize variables as None so they don't consume memory at startup
processor = None
model = None

def get_vision_pipeline():
    """Loads the model into memory only when needed for a scan"""
    global processor, model
    if processor is None or model is None:
        print("Loading Hugging Face Vision Pipeline...")
        model_name = "nateraw/vit-base-beans"
        try:
            # Attempt a standard online initialization check
            processor = AutoImageProcessor.from_pretrained(model_name)
            model = AutoModelForImageClassification.from_pretrained(model_name)
        except Exception:
            # Force isolated local file architecture pull if network getaddrinfo fails
            print("WARNING: Network connection unavailable. Pulling localized configuration parameters from storage cache...")
            processor = AutoImageProcessor.from_pretrained(model_name, local_files_only=True)
            model = AutoModelForImageClassification.from_pretrained(model_name, local_files_only=True)
    
    # Build the pipeline dynamically inside the function using the loaded models
    return pipeline("image-classification", model=model, image_processor=processor)

# Initialize Groq Client pulling safely from Render Environment Variables
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Database Model for Users
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    scans = db.relationship('PlantScan', backref='user', lazy=True)

# Database Model to store Scan History
class PlantScan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)
    diagnosis = db.Column(db.String(100), nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

with app.app_context():
    db.create_all()
    # Migration: Add user_id column if it doesn't exist
    try:
        from sqlalchemy import text
        db.session.execute(text("ALTER TABLE plant_scan ADD COLUMN user_id INTEGER REFERENCES user(id)"))
        db.session.commit()
    except Exception:
        db.session.rollback()

def send_otp_email(email, otp):
    smtp_server = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
    smtp_port = os.environ.get('SMTP_PORT', '587')

    # Assign the string directly instead of using os.environ.get()
    smtp_username = 'srirangamchamundeswari@gmail.com'
    smtp_password = 'flny osff ybze rera'  # Put your actual App Password here 
    smtp_sender = 'srirangamchamundeswari@gmail.com'

    subject = "SmartLeafAI - Your One-Time Verification Password (OTP)"
    body = f"Hello!\n\nYour SmartLeafAI verification OTP is: {otp}\n\nThis OTP is valid for 5 minutes. If you did not request this, please ignore this email.\n\nHappy Gardening,\nThe SmartLeafAI Team"

    if smtp_server and smtp_username and smtp_password:
        try:
            msg = MIMEMultipart()
            msg['From'] = smtp_sender
            msg['To'] = email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))

            server = smtplib.SMTP(smtp_server, int(smtp_port))
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
            server.quit()
            print(f"SMTP Success: OTP {otp} sent to {email}")
            return True
        except Exception as e:
            print(f"SMTP Error: Failed to send OTP to {email}. Error: {str(e)}")

    # Dev fallback printing
    print("\n" + "="*60)
    print(f"[SmartLeafAI OTP]: {otp} for email {email}")
    print("="*60 + "\n")
    return False

# Helper function to prevent repeating the timeline retrieval code
def get_user_scans(user_id):
    if not user_id:
        return []
    return PlantScan.query.filter_by(user_id=user_id).order_by(PlantScan.timestamp.desc()).all()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.path.startswith(('/ask_doctor', '/delete/', '/clear_all')):
                return jsonify({"success": False, "message": "Authentication required. Please log in."}), 401
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def home():
    user_id = session.get('user_id')
    if not user_id:
        if 'pending_register' in session or 'pending_login' in session:
            pending_email = ""
            if 'pending_register' in session:
                pending_email = session['pending_register']['email']
            elif 'pending_login' in session:
                pending_email = session['pending_login']['email']
            return render_template('index.html', logged_in=False, otp_flow=True, pending_email=pending_email)
        return render_template('index.html', logged_in=False)
    
    current_user = User.query.get(user_id)
    if not current_user:
        session.clear()
        return render_template('index.html', logged_in=False)
        
    return render_template('index.html', logged_in=True, username=current_user.username, scans=get_user_scans(user_id), active_scan=False)

@app.route('/register', methods=['POST'])
def register():
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')

    if not username or not email or not password:
        flash("All fields are required for registration.", "register_error")
        return render_template('index.html', logged_in=False, active_tab='register')

    # Check if user already exists
    existing_user_email = User.query.filter_by(email=email).first()
    if existing_user_email:
        flash("Email address is already registered.", "register_error")
        return render_template('index.html', logged_in=False, active_tab='register')

    existing_user_name = User.query.filter_by(username=username).first()
    if existing_user_name:
        flash("Username is already taken.", "register_error")
        return render_template('index.html', logged_in=False, active_tab='register')

    # Generate OTP and store details in session pending verification
    hashed_password = generate_password_hash(password, method='scrypt')
    otp = f"{random.randint(100000, 999999)}"
    
    session['pending_register'] = {
        'username': username,
        'email': email,
        'password': hashed_password,
        'otp': otp,
        'created_at': datetime.utcnow().timestamp()
    }
    session.pop('pending_login', None)

    is_sent = send_otp_email(email, otp)
    if not is_sent:
        flash(f"OTP generated (SMTP not configured). For testing, use OTP: {otp}", "otp_success")
    else:
        flash("A verification code has been sent to your email.", "otp_success")

    return redirect(url_for('home'))

@app.route('/login', methods=['POST'])
def login():
    login_id = request.form.get('login_id', '').strip()
    password = request.form.get('password', '')

    if not login_id or not password:
        flash("Please enter both your username/email and password.", "login_error")
        return render_template('index.html', logged_in=False, active_tab='login')

    # Find user by username or email
    user = User.query.filter((User.username == login_id) | (User.email == login_id)).first()

    if not user or not check_password_hash(user.password, password):
        flash("Invalid username/email or password.", "login_error")
        return render_template('index.html', logged_in=False, active_tab='login')

    # Direct Log-In without OTP flow
    session['user_id'] = user.id
    session['username'] = user.username
    session.pop('pending_register', None)
    session.pop('pending_login', None)
    
    flash("Successfully logged in!", "login_success")
    return redirect(url_for('home'))

@app.route('/verify_otp', methods=['POST'])
def verify_otp():
    otp_code = request.form.get('otp', '').strip()

    if not otp_code:
        flash("Please enter the verification code.", "otp_error")
        return redirect(url_for('home'))

    # Check active registration flow
    if 'pending_register' in session:
        data = session['pending_register']
        if datetime.utcnow().timestamp() - data['created_at'] > 300:
            session.pop('pending_register', None)
            flash("The verification code has expired. Please register again.", "register_error")
            return redirect(url_for('home'))

        if data['otp'] == otp_code:
            new_user = User(username=data['username'], email=data['email'], password=data['password'])
            try:
                db.session.add(new_user)
                db.session.commit()
                
                session['user_id'] = new_user.id
                session['username'] = new_user.username
                session.pop('pending_register', None)
                flash("Account successfully created!", "login_success")
                return redirect(url_for('home'))
            except Exception as e:
                db.session.rollback()
                flash(f"An database error occurred: {str(e)}", "otp_error")
                return redirect(url_for('home'))
        else:
            flash("Invalid verification code. Please try again.", "otp_error")
            return redirect(url_for('home'))

    # Check active login flow
    elif 'pending_login' in session:
        data = session['pending_login']
        if datetime.utcnow().timestamp() - data['created_at'] > 300:
            session.pop('pending_login', None)
            flash("The verification code has expired. Please sign in again.", "login_error")
            return redirect(url_for('home'))

        if data['otp'] == otp_code:
            session['user_id'] = data['user_id']
            session['username'] = data['username']
            session.pop('pending_login', None)
            return redirect(url_for('home'))
        else:
            flash("Invalid verification code. Please try again.", "otp_error")
            return redirect(url_for('home'))

    flash("No active verification session found. Please sign in or register.", "login_error")
    return redirect(url_for('home'))

@app.route('/resend_otp')
def resend_otp():
    if 'pending_register' in session:
        data = session['pending_register']
        otp = f"{random.randint(100000, 999999)}"
        data['otp'] = otp
        data['created_at'] = datetime.utcnow().timestamp()
        session['pending_register'] = data

        is_sent = send_otp_email(data['email'], otp)
        if not is_sent:
            flash(f"New OTP generated (SMTP not configured). For testing, use OTP: {otp}", "otp_success")
        else:
            flash("A new verification code has been sent to your email.", "otp_success")
        return redirect(url_for('home'))

    elif 'pending_login' in session:
        data = session['pending_login']
        otp = f"{random.randint(100000, 999999)}"
        data['otp'] = otp
        data['created_at'] = datetime.utcnow().timestamp()
        session['pending_login'] = data

        is_sent = send_otp_email(data['email'], otp)
        if not is_sent:
            flash(f"New OTP generated (SMTP not configured). For testing, use OTP: {otp}", "otp_success")
        else:
            flash("A new verification code has been sent to your email.", "otp_success")
        return redirect(url_for('home'))

    flash("No verification session in progress.", "login_error")
    return redirect(url_for('home'))

@app.route('/cancel_otp')
def cancel_otp():
    session.pop('pending_register', None)
    session.pop('pending_login', None)
    flash("Verification cancelled.", "login_error")
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/predict', methods=['POST'])
@login_required
def predict():
    user_id = session['user_id']
    current_user = User.query.get(user_id)
    
    if 'file' not in request.files:
        return render_template('index.html', logged_in=True, username=current_user.username, error_message="No file uploaded", scans=get_user_scans(user_id), active_scan=False)
        
    file = request.files['file']
    if file.filename == '':
        return render_template('index.html', logged_in=True, username=current_user.username, error_message="No selected file", scans=get_user_scans(user_id), active_scan=False)
    
    if file:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filepath)
        
        # 🔬 Load pipeline dynamically on-demand right here!
        classifier_pipeline = get_vision_pipeline()
        image = Image.open(filepath)
        predictions = classifier_pipeline(image)
        top_pred = predictions[0]
        
        disease = top_pred['label']
        confidence = round(top_pred['score'] * 100, 2)
        
        # 🛡️ THE VALIDATION GUARDRAIL
        SAFE_THRESHOLD = 55.0
        if confidence < SAFE_THRESHOLD:
            if os.path.exists(filepath):
                os.remove(filepath) # Remove invalid image from disk storage instantly
                
            error_msg = f"Validation Guardrail Triggered: The uploaded photo does not look like a plant leaf (Match confidence was too low: {confidence}%). Please clear your frame and scan a valid plant foliage leaf."
            return render_template('index.html', 
                                   logged_in=True,
                                   username=current_user.username,
                                   error_message=error_msg, 
                                   scans=get_user_scans(user_id), 
                                   active_scan=False)
        
        # ✅ It passed! Save valid plant scan to database history records
        new_scan = PlantScan(filename=file.filename, diagnosis=disease, confidence=confidence, user_id=user_id)
        db.session.add(new_scan)
        db.session.commit()
        
        return render_template('index.html', 
                               logged_in=True,
                               username=current_user.username,
                               active_scan=True, 
                               disease=disease, 
                               confidence=confidence, 
                               image_path=filepath,
                               scans=get_user_scans(user_id))

@app.route('/ask_doctor', methods=['POST'])
@login_required
def ask_doctor():
    """Handles conversational questions using explicit language dropdown controls"""
    user_data = request.json
    user_message = user_data.get('message')
    context_disease = user_data.get('disease')
    target_language = user_data.get('language', 'English')
    
    system_prompt = (
        f"You are 'Leafy', an expert AI Plant Doctor. The user's plant leaf was diagnosed with {context_disease}. "
        f"Answer the user's question expertly and comprehensively. "
        f"Always make sure to mention: 1. Actionable Chemical Treatment, 2. Organic Prevention Methods, and 3. An Urgency Scale (Low/Medium/High). "
        f"CRITICAL CONSTRAINT: You MUST write your entire response exclusively in {target_language}. "
        f"Do not respond in any other language than {target_language}."
    )
    
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            model="llama-3.1-8b-instant",
        )
        return jsonify({"response": chat_completion.choices[0].message.content})
    except Exception as e:
        return jsonify({"response": f"Error linking with Groq AI Doctor: {str(e)}"}), 500

@app.route('/delete/<int:scan_id>', methods=['POST'])
@login_required
def delete_scan(scan_id):
    """Deletes a specific history record from the SQLite database"""
    scan_to_delete = PlantScan.query.get_or_404(scan_id)
    if scan_to_delete.user_id != session['user_id']:
        return jsonify({"success": False, "message": "Unauthorized access to this record"}), 403
    try:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], scan_to_delete.filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            
        db.session.delete(scan_to_delete)
        db.session.commit()
        return jsonify({"success": True, "message": "Record deleted successfully"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/clear_all', methods=['POST'])
@login_required
def clear_all_history():
    """Wipes out the user's scan history records"""
    user_id = session['user_id']
    try:
        scans_to_delete = PlantScan.query.filter_by(user_id=user_id).all()
        for scan in scans_to_delete:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], scan.filename)
            if os.path.exists(file_path):
                os.remove(file_path)
            db.session.delete(scan)
            
        db.session.commit()
        return jsonify({"success": True, "message": "All history cleared"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)}), 500

from waitress import serve

if __name__ == "__main__":
    print("Server is starting...")
    print("Open: http://127.0.0.1:5000")
    serve(app, host="0.0.0.0", port=5000)
