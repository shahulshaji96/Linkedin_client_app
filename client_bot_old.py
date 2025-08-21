import os
import json
import time
import threading
import csv
from datetime import datetime
from flask import Flask, request, jsonify
import requests
from linkedin_automation import LinkedInAutomation
import logging
import uuid
from collections import defaultdict
import PySimpleGUI as sg
import sys
import signal
import atexit
from pyngrok import ngrok
import random
import google.generativeai as genai

# Import all functions from LinkedIn_automation_script.py
from urllib.parse import quote_plus
import tempfile
import platform
import shutil

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('client_automation.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class EnhancedLinkedInAutomationClient:
    def __init__(self):
        self.config_file = "client_config.json"
        self.config = self.load_or_create_config()
        
        # Exit if config creation was cancelled
        if self.config is None:
            logger.error("❌ Configuration setup was cancelled or failed")
            sys.exit(1)
        
        self.automation_instances = {}
        self.active_campaigns = defaultdict(lambda: {
            'user_action': None, 
            'awaiting_confirmation': False,
            'current_contact': None,
            'status': 'idle'
        })
        self.flask_app = None
        self.flask_thread = None
        self.running = False
        
        # Initialize Gemini AI
        try:
            gemini_api_key = self.config.get('gemini_api_key')
            if not gemini_api_key:
                logger.error("❌ No Gemini API key found in configuration")
                self.model = None
            else:
                genai.configure(api_key=gemini_api_key)
                self.model = genai.GenerativeModel('gemini-1.5-flash')
                logger.info("✅ Gemini AI initialized successfully")
        except Exception as e:
            logger.error(f"❌ Gemini AI initialization failed: {e}")
            self.model = None

        # Setup ngrok tunnel and register with dashboard
        try:
            local_port = self.config.get('local_port', 5001)
            dashboard_url = self.config.get('dashboard_url')
            
            if not dashboard_url:
                logger.error("❌ No dashboard URL found in configuration")
                return
                
            public_url = ngrok.connect(local_port, bind_tls=True).public_url
            logger.info(f"🔗 Public tunnel URL: {public_url}")
            
            logger.info(f"📡 Attempting to register with dashboard at: {dashboard_url}")
            
            # Create the payload with the client URL and the secret Gemini API key
            registration_payload = {
                'client_url': public_url,
                'gemini_api_key': self.config.get('gemini_api_key')
            }
            
            # Call the new, unauthenticated endpoint
            response = requests.post(
                f"{dashboard_url}/api/register_client_bot",  # <-- USE THE NEW ENDPOINT
                headers={'Content-Type': 'application/json'},
                json=registration_payload,
                timeout=45,  # Increased timeout for cold starts on Render
                verify=True
            )
            
            if response.status_code == 200:
                logger.info("✅ Successfully registered client URL with dashboard")
            else:
                logger.warning(f"⚠️ Dashboard registration failed with status {response.status_code}")
                logger.warning(f"Response: {response.text}")
                
        except requests.exceptions.Timeout:
            logger.error(f"❌ Timeout connecting to dashboard at {dashboard_url}")
            logger.error("This could be because the dashboard server is slow to respond (cold start) or unavailable.")
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Connection error to dashboard at {dashboard_url}: {e}")
            logger.error("This could be because the dashboard server is down or the URL is incorrect.")
        except Exception as e:
            logger.error(f"❌ Failed to setup ngrok or register client URL: {e}")

        self.setup_flask_app()

    def load_or_create_config(self):
        """Load existing config or create new one via GUI"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                logger.info("✅ Configuration loaded successfully")
                return config
            except Exception as e:
                logger.error(f"❌ Error loading config: {e}")
        
        logger.info("📋 No configuration found, launching setup GUI...")
        return self.create_config_gui()

    def create_config_gui(self):
        """Create configuration GUI with dashboard URL options"""
        sg.theme('DarkBlue3')
        
        # Default values
        default_dashboard = "https://linkedin-automation-dashboard.onrender.com"
        
        layout = [
            [sg.Text('LinkedIn Automation Client Setup', font=('Helvetica', 16, 'bold'))],
            [sg.Text('')],
            [sg.Text('LinkedIn Credentials:', font=('Helvetica', 12, 'bold'))],
            [sg.Text('Email:', size=(15,1)), sg.Input(key='linkedin_email', size=(30,1))],
            [sg.Text('Password:', size=(15,1)), sg.Input(key='linkedin_password', password_char='*', size=(30,1))],
            [sg.Text('')],
            [sg.Text('AI Configuration:', font=('Helvetica', 12, 'bold'))],
            [sg.Text('Gemini API Key:', size=(15,1)), sg.Input(key='gemini_api_key', size=(30,1))],
            [sg.Text('')],
            [sg.Text('Client Settings:', font=('Helvetica', 12, 'bold'))],
            [sg.Text('Local Port:', size=(15,1)), sg.Input('5001', key='local_port', size=(10,1))],
            [sg.Text('')],
            [sg.Text('Dashboard Connection:', font=('Helvetica', 12, 'bold'))],
            [sg.Radio('Online Dashboard (Render)', 'dashboard_type', default=True, key='use_online')],
            [sg.Radio('Local Dashboard', 'dashboard_type', key='use_local')],
            [sg.Text('Dashboard URL:', size=(15,1)), sg.Input(default_dashboard, key='dashboard_url', size=(50,1))],
            [sg.Text('(Use https://linkedin-automation-dashboard.onrender.com for online)', font=('Helvetica', 8))],
            [sg.Text('(Use http://127.0.0.1:5000 for local development)', font=('Helvetica', 8))],
            [sg.Text('')],
            [sg.Button('Save & Start', size=(12,1)), sg.Button('Cancel', size=(12,1))]
        ]

        window = sg.Window('LinkedIn Automation Client Setup', layout, finalize=True)
    def update_dashboard_url():
        values = window.read(timeout=0)[1]
        if values and isinstance(values, dict):
            if values.get('use_online'):
                window['dashboard_url'].update('https://linkedin-automation-dashboard.onrender.com')
            elif values.get('use_local'):
                window['dashboard_url'].update('http://127.0.0.1:5000')
        
        try:
            while True:
                event, values = window.read(timeout=500)
                
                if event in (sg.WIN_CLOSED, 'Cancel'):
                    logger.info("👋 Configuration setup cancelled by user")
                    window.close()
                    return None  # Return None instead of sys.exit
                
                if event == sg.TIMEOUT_EVENT:
                    # Check for radio button changes
                    continue
                    
                if event in ['use_online', 'use_local']:
                    update_dashboard_url()
                    continue
                
                if event == 'Save & Start':
                    # Validate inputs
                    if not values or not isinstance(values, dict):
                        sg.popup_error('Error reading form values!')
                        continue
                        
                    required_fields = ['linkedin_email', 'linkedin_password', 'gemini_api_key']
                    missing_fields = [field for field in required_fields if not values.get(field, '').strip()]
                    
                    if missing_fields:
                        sg.popup_error(f'Please fill in all required fields: {", ".join(missing_fields)}')
                        continue
                    
                    dashboard_url = values.get('dashboard_url', '').strip()
                    if not dashboard_url:
                        sg.popup_error('Please enter a dashboard URL!')
                        continue
                    
                    # Validate URL format
                    if not (dashboard_url.startswith('http://') or dashboard_url.startswith('https://')):
                        sg.popup_error('Dashboard URL must start with http:// or https://')
                        continue
                    
                    # Validate port
                    try:
                        local_port = int(values.get('local_port', 5001))
                        if local_port < 1 or local_port > 65535:
                            raise ValueError("Port must be between 1 and 65535")
                    except (ValueError, TypeError):
                        sg.popup_error('Please enter a valid port number (1-65535)!')
                        continue
                    
                    config = {
                        'linkedin_email': values['linkedin_email'].strip(),
                        'linkedin_password': values['linkedin_password'].strip(),
                        'gemini_api_key': values['gemini_api_key'].strip(),
                        'local_port': local_port,
                        'dashboard_url': dashboard_url,
                        'use_online_dashboard': values.get('use_online', True),
                        'created_at': datetime.now().isoformat()
                    }
                    
                    # Test connection to dashboard before saving
                    try:
                        logger.info(f"🧪 Testing connection to {dashboard_url}...")
                        test_response = requests.get(f"{dashboard_url}/", timeout=15)
                        if test_response.status_code == 200:
                            logger.info("✅ Dashboard connection test successful")
                        else:
                            logger.warning(f"⚠️ Dashboard responded with status {test_response.status_code}")
                            result = sg.popup_yes_no(
                                f'Dashboard connection test failed (status {test_response.status_code}).\n'
                                'Do you want to continue anyway?',
                                title='Connection Test Failed'
                            )
                            if result == 'No':
                                continue
                    except requests.exceptions.Timeout:
                        result = sg.popup_yes_no(
                            'Dashboard connection timeout! This might be normal for online servers.\n'
                            'Do you want to continue anyway?',
                            title='Connection Timeout'
                        )
                        if result == 'No':
                            continue
                    except requests.exceptions.ConnectionError:
                        result = sg.popup_yes_no(
                            'Cannot connect to dashboard! Please check the URL and your internet connection.\n'
                            'Do you want to continue anyway?',
                            title='Connection Error'
                        )
                        if result == 'No':
                            continue
                    except Exception as e:
                        result = sg.popup_yes_no(
                            f'Dashboard connection test failed: {str(e)}\n'
                            'Do you want to continue anyway?',
                            title='Connection Test Error'
                        )
                        if result == 'No':
                            continue
                    
                    # Save config
                    try:
                        with open(self.config_file, 'w') as f:
                            json.dump(config, f, indent=2)
                        logger.info("✅ Configuration saved successfully")
                        window.close()
                        return config
                    except Exception as e:
                        sg.popup_error(f'Error saving configuration: {e}')
                        continue
        
        except Exception as e:
            logger.error(f"❌ Error in configuration GUI: {e}")
            try:
                window.close()
            except:
                pass
            return None

    def setup_flask_app(self):
        """Setup Flask app for receiving requests from dashboard"""
        self.flask_app = Flask(__name__)

        @self.flask_app.route('/health', methods=['GET'])
        def health_check():
            return jsonify({
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'active_campaigns': len(self.active_campaigns),
                'version': '2.0.0',
                'dashboard_url': self.config.get('dashboard_url', 'unknown')
            })

        @self.flask_app.route('/start_campaign', methods=['POST'])
        def start_campaign():
            try:
                data = request.json
                campaign_id = data.get('campaign_id', str(uuid.uuid4()))
                user_config = data.get('user_config', {})
                campaign_data = data.get('campaign_data', {})
                
                logger.info(f"🚀 Starting campaign: {campaign_id}")
                
                # Start campaign in background thread
                campaign_thread = threading.Thread(
                    target=self.run_enhanced_outreach_campaign,
                    args=(campaign_id, user_config, campaign_data),
                    daemon=True
                )
                campaign_thread.start()
                
                return jsonify({
                    'success': True,
                    'campaign_id': campaign_id,
                    'message': 'Campaign started successfully'
                })
                
            except Exception as e:
                logger.error(f"❌ Error starting campaign: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.flask_app.route('/keyword_search', methods=['POST'])
        def keyword_search():
            try:
                data = request.json
                search_id = data.get('search_id', str(uuid.uuid4()))
                user_config = data.get('user_config', {})
                search_params = data.get('search_params', {})
                
                logger.info(f"🔍 Starting keyword search: {search_id}")
                
                # Start search in background thread
                search_thread = threading.Thread(
                    target=self.run_enhanced_keyword_search,
                    args=(search_id, user_config, search_params),
                    daemon=True
                )
                search_thread.start()
                
                return jsonify({
                    'success': True,
                    'search_id': search_id,
                    'message': 'Keyword search started successfully'
                })
                
            except Exception as e:
                logger.error(f"❌ Error starting keyword search: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.flask_app.route('/process_inbox', methods=['POST'])
        def process_inbox():
            try:
                data = request.json
                process_id = data.get('process_id', str(uuid.uuid4()))
                user_config = data.get('user_config', {})
                
                logger.info(f"📬 Starting inbox processing: {process_id}")
                
                # Start inbox processing in background thread
                inbox_thread = threading.Thread(
                    target=self.run_enhanced_inbox_processing,
                    args=(process_id, user_config),
                    daemon=True
                )
                inbox_thread.start()
                
                return jsonify({
                    'success': True,
                    'process_id': process_id,
                    'message': 'Inbox processing started successfully'
                })
                
            except Exception as e:
                logger.error(f"❌ Error starting inbox processing: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.flask_app.route('/campaign_status/<campaign_id>', methods=['GET'])
        def get_campaign_status(campaign_id):
            status = self.active_campaigns.get(campaign_id, {})
            # Don't send the full user_action object back, just the status
            status_copy = status.copy()
            status_copy.pop('user_action', None)
            return jsonify(status_copy)

        @self.flask_app.route('/campaign_action', methods=['POST'])
        def campaign_action():
            try:
                data = request.json
                campaign_id = data.get('campaign_id')
                action = data.get('action')
                message = data.get('message')
                contact_index = data.get('contact_index')
                
                if campaign_id in self.active_campaigns:
                    self.active_campaigns[campaign_id]['user_action'] = {
                        'action': action,
                        'message': message,
                        'contact_index': contact_index,
                        'timestamp': datetime.now().isoformat()
                    }
                    
                    # Resume campaign processing
                    self.active_campaigns[campaign_id]['awaiting_confirmation'] = False
                    
                    logger.info(f"✅ Received action '{action}' for campaign {campaign_id}")
                    return jsonify({'success': True})
                
                return jsonify({'success': False, 'error': 'Campaign not found'}), 404
                
            except Exception as e:
                logger.error(f"❌ Error processing campaign action: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.flask_app.route('/stop_campaign/<campaign_id>', methods=['POST'])
        def stop_campaign(campaign_id):
            if campaign_id in self.active_campaigns:
                self.active_campaigns[campaign_id]['stop_requested'] = True
                return jsonify({'success': True, 'message': 'Stop request sent'})
            return jsonify({'success': False, 'error': 'Campaign not found'}), 404

    # ... (rest of the methods remain the same) ...

    def report_progress_to_dashboard(self, campaign_id, final=False):
        """Report campaign progress back to dashboard with better error handling"""
        try:
            dashboard_url = self.config.get('dashboard_url')
            if not dashboard_url:
                logger.debug("No dashboard URL configured")
                return

            progress_data = self.active_campaigns.get(campaign_id, {})
            
            # Include current contact info if awaiting confirmation
            if progress_data.get('awaiting_confirmation') and progress_data.get('current_contact'):
                progress_data['awaiting_action'] = True
                progress_data['current_contact_preview'] = progress_data['current_contact']
            
            endpoint = f"{dashboard_url}/api/campaign_progress"
            
            response = requests.post(endpoint, json={
                'campaign_id': campaign_id,
                'progress': progress_data,
                'final': final
            }, timeout=30, verify=True)  # Increased timeout and enable SSL verification
            
            if response.status_code == 200:
                logger.debug(f"✅ Successfully reported progress for campaign {campaign_id}")
            else:
                logger.warning(f"⚠️ Dashboard progress report returned status {response.status_code}")

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout reporting progress to dashboard for campaign {campaign_id}")
        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠️ Connection error reporting progress to dashboard for campaign {campaign_id}")
        except Exception as e:
            logger.debug(f"Could not report progress for campaign {campaign_id}: {e}")

    def report_search_results_to_dashboard(self, search_id, results):
        """Report search results back to dashboard with better error handling"""
        try:
            dashboard_url = self.config.get('dashboard_url')
            if not dashboard_url:
                return

            endpoint = f"{dashboard_url}/api/search_results"
            
            response = requests.post(endpoint, json={
                'search_id': search_id,
                'results': results
            }, timeout=30, verify=True)
            
            if response.status_code == 200:
                logger.info(f"✅ Successfully reported search results for {search_id}")
            else:
                logger.warning(f"⚠️ Dashboard search report returned status {response.status_code}")

        except Exception as e:
            logger.debug(f"Could not report search results for {search_id}: {e}")

    def report_inbox_results_to_dashboard(self, process_id, results):
        """Report inbox processing results back to dashboard with better error handling"""
        try:
            dashboard_url = self.config.get('dashboard_url')
            if not dashboard_url:
                return

            endpoint = f"{dashboard_url}/api/inbox_results"
            
            response = requests.post(endpoint, json={
                'process_id': process_id,
                'results': results
            }, timeout=30, verify=True)
            
            if response.status_code == 200:
                logger.info(f"✅ Successfully reported inbox results for {process_id}")
            else:
                logger.warning(f"⚠️ Dashboard inbox report returned status {response.status_code}")

        except Exception as e:
            logger.debug(f"Could not report inbox results for {process_id}: {e}")
    # ==============================================
    # ENHANCED LINKEDIN AUTOMATION FUNCTIONS
    # ==============================================

    def initialize_browser(self):
        """Initialize Chrome browser with optimal settings"""
        from selenium import webdriver
        
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        # Create persistent profile directory
        profile_dir = os.path.join(tempfile.gettempdir(), "linkedin_automation_profile")
        options.add_argument(f"--user-data-dir={profile_dir}")
        
        driver = webdriver.Chrome(options=options)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        return driver

    def human_delay(self, min_seconds=1, max_seconds=3):
        """Add human-like delays"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)

    def type_like_human(self, element, text):
        """Type text with human-like delays"""
        element.clear()
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.05, 0.2))

    def login(self):
        """Enhanced login with session validation and persistence - ORIGINAL APPROACH"""
        try:
            logger.info("🔐 Checking LinkedIn session...")
            
            # First, navigate to LinkedIn feed to check existing session
            self.driver.get("https://www.linkedin.com/feed")
            time.sleep(3)
            
            # Check if already logged in by looking for navigation elements
            if self._is_logged_in():
                logger.info("✅ Already logged in! Session restored successfully")
                return True
            
            logger.info("🔄 No active session found, attempting login...")
            
            # Navigate to login page
            self.driver.get("https://www.linkedin.com/login")
            self.wait.until(EC.presence_of_element_located((By.ID, "username")))
            self.human_delay(1.5, 3)
            
            # Type email
            username_field = self.driver.find_element(By.ID, "username")
            logger.info("✏️ Typing email...")
            self.type_like_human(username_field, self.email)
            self.human_delay(1, 2)
            
            # Type password
            password_field = self.driver.find_element(By.ID, "password")
            logger.info("✏️ Typing password...")
            self.type_like_human(password_field, self.password)
            
            # Click Login
            login_button = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            self.safe_click(login_button)
            
            # Wait for login success - check for feed or profile URL
            try:
                self.wait.until(lambda d: self._is_logged_in(), timeout=30)
                logger.info("✅ LinkedIn login successful!")
                self.human_delay(2, 4)
                return True
                
            except TimeoutException:
                if "checkpoint" in self.driver.current_url or "challenge" in self.driver.current_url:
                    logger.warning("⚠️ 2FA/Challenge page detected. Please complete manually.")
                    # Wait for user to complete 2FA
                    logger.info("⏳ Waiting for manual 2FA completion...")
                    for i in range(120):  # Wait up to 2 minutes
                        time.sleep(1)
                        if self._is_logged_in():
                            logger.info("✅ 2FA completed successfully!")
                            return True
                    logger.error("❌ 2FA timeout - please try again")
                    return False
                logger.error("❌ Login failed or timed out.")
                return False
                
        except Exception as e:
            logger.error(f"❌ Login exception: {e}")
            return False

    def _is_logged_in(self):
        """Enhanced login status check"""
        try:
            current_url = self.driver.current_url
            
            # Check URL patterns for successful login
            if any(pattern in current_url for pattern in [
                "linkedin.com/feed",
                "linkedin.com/in/",
                "linkedin.com/mynetwork", 
                "linkedin.com/jobs",
                "linkedin.com/messaging"
            ]):
                return True
                
            # Check for navigation elements
            nav_selectors = [
                "[data-test-id='global-nav']",
                ".global-nav",
                ".global-nav__nav",
                "nav.global-nav"
            ]
            
            for selector in nav_selectors:
                if len(self.driver.find_elements(By.CSS_SELECTOR, selector)) > 0:
                    return True
                    
            # Check for profile icon
            profile_selectors = [
                ".global-nav__primary-item--profile",
                ".global-nav__me-photo", 
                "[data-test-id='nav-profile-photo']"
            ]
            
            for selector in profile_selectors:
                if len(self.driver.find_elements(By.CSS_SELECTOR, selector)) > 0:
                    return True
                    
            return False
        except Exception:
            return False


    def extract_profile_data(self, driver):
        """Extract profile data from LinkedIn profile page"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import NoSuchElementException, TimeoutException
        
        profile_data = {}
        try:
            WebDriverWait(driver, 10).until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "h1")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".pv-text-details__left-panel"))
                )
            )

            # Extract name
            name_selectors = [
                "h1.text-heading-xlarge",
                ".pv-text-details__left-panel h1",
                "[data-test-id='profile-name'] h1",
                ".ph5 h1"
            ]
            
            for selector in name_selectors:
                try:
                    name_elem = driver.find_element(By.CSS_SELECTOR, selector)
                    profile_data['extracted_name'] = name_elem.text.strip()
                    logger.info(f"📝 Extracted name: {profile_data['extracted_name']}")
                    break
                except NoSuchElementException:
                    continue

            # Extract headline
            headline_selectors = [
                ".text-body-medium.break-words",
                ".pv-text-details__left-panel .text-body-medium",
                "[data-test-id='profile-headline']"
            ]
            
            for selector in headline_selectors:
                try:
                    headline_elem = driver.find_element(By.CSS_SELECTOR, selector)
                    headline_text = headline_elem.text.strip()
                    if headline_text and headline_text != profile_data.get('extracted_name', ''):
                        profile_data['extracted_headline'] = headline_text
                        logger.info(f"💼 Extracted headline: {headline_text[:50]}...")
                        break
                except NoSuchElementException:
                    continue

            # Extract about section
            about_selectors = [
                "[data-test-id='about-section'] .pv-shared-text-with-see-more span[aria-hidden='true']",
                ".pv-about-section .pv-shared-text-with-see-more span"
            ]
            
            for selector in about_selectors:
                try:
                    about_elem = driver.find_element(By.CSS_SELECTOR, selector)
                    about_text = about_elem.text.strip()
                    if about_text:
                        profile_data['about_snippet'] = about_text[:150] + "..." if len(about_text) > 150 else about_text
                        logger.info(f"📄 Extracted about: {profile_data['about_snippet'][:50]}...")
                        break
                except NoSuchElementException:
                    continue

            # Set defaults
            if not profile_data.get('extracted_name'):
                profile_data['extracted_name'] = "Professional"
            if not profile_data.get('about_snippet'):
                profile_data['about_snippet'] = ""

        except Exception as e:
            logger.warning(f"⚠️ Profile data extraction failed: {e}")
            profile_data = {
                'extracted_name': 'Professional',
                'extracted_headline': '',
                'about_snippet': ''
            }

        return profile_data

    def generate_message(self, name, company, role, service_1, service_2, profile_data=None):
        """Generate personalized message using AI"""
        if not self.model:
            fallback_msg = f"Hi {name}, I'm impressed by your work as {role} at {company}. I'd love to connect and learn more about your experience. Looking forward to connecting!"
            return fallback_msg[:280]

        actual_name = profile_data.get('extracted_name', name) if profile_data else name
        about_snippet = profile_data.get('about_snippet', '') if profile_data else ''

        MESSAGE_TEMPLATE = """Create a personalized LinkedIn connection message based on the profile information provided.

Profile Information:
- Name: {Name}
- Company: {Company}  
- Role: {Role}
- Services/Expertise: {service_1}, {service_2}
- About/Bio: {about_snippet}

Create a professional, engaging message under 280 characters that:
1. Addresses them by name (ONLY USE FIRST NAMES)
2. References their specific work/company
3. Mentions a relevant connection point
4. Has a clear call to action

Return ONLY the message text, no labels or formatting.
"""

        prompt = MESSAGE_TEMPLATE.format(
            Name=actual_name,
            Company=company,
            Role=role,
            service_1=service_1 or "your field",
            service_2=service_2 or "industry trends",
            about_snippet=about_snippet
        )

        for attempt in range(3):
            try:
                response = self.model.generate_content(prompt)
                message = response.text.strip()
                message = re.sub(r'^(Icebreaker:|Message:)\s*', '', message, flags=re.IGNORECASE)
                message = message.strip('"\'[]')
                
                if len(message) > 280:
                    message = message[:277] + "..."
                
                return message
                
            except Exception as e:
                if "429" in str(e) or "ResourceExhausted" in str(e):
                    wait_time = 30 * (attempt + 1)
                    logger.warning(f"⏳ Gemini rate limit hit. Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ Gemini error: {e}")
                    break

        # Fallback message
        fallback_msg = f"Hi {actual_name}, I'm impressed by your {role} work at {company}. I'd love to connect and exchange insights. Looking forward to connecting!"
        return fallback_msg[:280]

    def safe_click(self, driver, element):
        """Safely click an element with fallback methods"""
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.common.exceptions import ElementClickInterceptedException, ElementNotInteractableException
        
        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", element)
            time.sleep(random.uniform(0.5, 1.5))
            element.click()
            return True
        except (ElementClickInterceptedException, ElementNotInteractableException):
            try:
                ActionChains(driver).move_to_element(element).pause(0.5).click().perform()
                return True
            except Exception as e:
                logger.warning(f"Click fallback failed: {e}")
                return False
        except Exception as e:
            logger.warning(f"Click failed: {e}")
            return False

    def find_element_safe(self, driver, selectors, timeout=10):
        """Find element using multiple selectors"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException
        
        for selector_type, selector in selectors:
            try:
                if selector_type == "xpath":
                    element = WebDriverWait(driver, timeout).until(
                        EC.presence_of_element_located((By.XPATH, selector))
                    )
                else:
                    element = WebDriverWait(driver, timeout).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                return element
            except TimeoutException:
                continue
        return None

    def send_connection_request_with_note(self, message, name):
        """Send connection request with personalized note"""
        from selenium.webdriver.common.by import By
        from selenium.common.exceptions import TimeoutException
        
        logger.info(f"🤝 Attempting to send connection request with note to {name}...")

        # Find Connect button
        connect_button_selectors = [
            ("css", "button.artdeco-button.artdeco-button--2.artdeco-button--primary[aria-label*='Connect']"),
            ("xpath", "//button[contains(@aria-label, 'Connect') and contains(@class, 'artdeco-button--primary')]"),
            ("xpath", "//button[.//span[text()='Connect']]"),
            ("css", "button[aria-label*='Connect'][class*='artdeco-button']")
        ]

        connect_button = self.find_element_safe(driver, connect_button_selectors, timeout=8)
        if not connect_button:
            logger.error("❌ Connect button not found")
            return False

        # Click Connect button
        if not self.safe_click(driver, connect_button):
            logger.error("❌ Failed to click Connect button")
            return False

        logger.info("✅ Connect button clicked")
        self.human_delay(2, 3)

        try:
            # Look for "Add a note" button
            add_note_selectors = [
                ("css", "button[aria-label='Add a note']"),
                ("xpath", "//button[@aria-label='Add a note']"),
                ("xpath", "//button[.//span[text()='Add a note']]"),
                ("css", "button[aria-label*='Add a note']"),
                ("xpath", "//button[contains(text(), 'Add a note')]")
            ]

            add_note_button = self.find_element_safe(driver, add_note_selectors, timeout=8)
            if not add_note_button:
                logger.info("❌ Add a note button not found - cannot send with note")
                return False

            # Click "Add a note"
            if not self.safe_click(driver, add_note_button):
                logger.error("❌ Failed to click Add a note button")
                return False

            logger.info("✅ Add a note clicked")
            self.human_delay(1, 2)

            # Find and fill note text area
            note_area_selectors = [
                ("css", "textarea[name='message']"),
                ("css", "#custom-message"),
                ("css", "textarea[aria-label*='note']"),
                ("css", ".connect-note-form textarea"),
                ("xpath", "//textarea[@name='message']")
            ]

            note_area = self.find_element_safe(driver, note_area_selectors, timeout=8)
            if not note_area:
                logger.error("❌ Could not find note text area")
                return False

            # Type the personalized message
            self.type_like_human(note_area, message)
            logger.info("✅ Personalized note added successfully")
            self.human_delay(1, 2)

            # Find and click Send button
            send_request_selectors = [
                ("css", "button[aria-label='Send now']"),
                ("xpath", "//button[@aria-label='Send now']"),
                ("css", "button[aria-label*='Send invitation']"),
                ("xpath", "//button[contains(@aria-label, 'Send')]"),
                ("xpath", "//button[.//span[text()='Send']]")
            ]

            send_button = self.find_element_safe(driver, send_request_selectors, timeout=10)
            if send_button and self.safe_click(driver, send_button):
                logger.info(f"✅ Connection request with note sent successfully to {name}!")
                self.human_delay(2, 4)
                return True
            else:
                logger.error("❌ Could not find or click send button")
                return False

        except Exception as e:
            logger.error(f"❌ Error sending connection request with note: {e}")
            return False

    def send_connection_request_without_note(self, name):
        """Send connection request without personalized note"""
        from selenium.webdriver.common.by import By
        
        logger.info(f"🤝 Attempting to send connection request without note to {name}...")

        # Find Connect button (same logic as with note)
        connect_button_selectors = [
            ("css", "button.artdeco-button.artdeco-button--2.artdeco-button--primary[aria-label*='Connect']"),
            ("xpath", "//button[contains(@aria-label, 'Connect') and contains(@class, 'artdeco-button--primary')]"),
            ("xpath", "//button[.//span[text()='Connect']]"),
            ("css", "button[aria-label*='Connect'][class*='artdeco-button']")
        ]

        connect_button = self.find_element_safe(driver, connect_button_selectors, timeout=8)
        if not connect_button:
            logger.error("❌ Connect button not found")
            return False

        # Click Connect button
        if not self.safe_click(driver, connect_button):
            logger.error("❌ Failed to click Connect button")
            return False

        logger.info("✅ Connect button clicked")
        self.human_delay(2, 3)

        try:
            # Look for Send button (skip adding note)
            send_request_selectors = [
                ("css", "button[aria-label='Send now']"),
                ("xpath", "//button[@aria-label='Send now']"),
                ("css", "button[aria-label*='Send invitation']"),
                ("xpath", "//button[contains(@aria-label, 'Send') and contains(@class, 'artdeco-button--primary')]"),
                ("xpath", "//button[.//span[text()='Send']]"),
                ("css", "button.artdeco-button--primary[aria-label*='Send']")
            ]

            send_button = self.find_element_safe(driver, send_request_selectors, timeout=10)
            if send_button and self.safe_click(driver, send_button):
                logger.info(f"✅ Connection request without note sent successfully to {name}!")
                self.human_delay(2, 4)
                return True
            else:
                logger.error("❌ Could not find or click send button")
                return False

        except Exception as e:
            logger.error(f"❌ Error sending connection request without note: {e}")
            return False

    def send_direct_message(self, message, name):
        """Send direct message to LinkedIn connection"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, NoSuchElementException
        from selenium.webdriver.common.action_chains import ActionChains
        
        logger.info(f"🔍 Attempting to locate Message button for {name}...")

        # Multiple selector strategies for the Message button
        message_button_selectors = [
            ("css", "button[aria-label*='Message'][class*='artdeco-button']"),
            ("css", "button.artdeco-button--primary[aria-label*='Message']"),
            ("xpath", "//button[contains(@aria-label, 'Message') and contains(@class, 'artdeco-button')]"),
            ("xpath", "//button[.//span[text()='Message']]"),
            ("css", "button[data-control-name*='message']"),
            ("css", "button[aria-label*='Message']"),
            ("xpath", "//button[contains(text(), 'Message')]")
        ]

        msg_btn = None
        for selector_type, selector in message_button_selectors:
            try:
                if selector_type == "xpath":
                    msg_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    msg_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )

                if msg_btn and msg_btn.is_displayed() and msg_btn.is_enabled():
                    logger.info(f"✅ Message button found using: {selector}")
                    break
                else:
                    msg_btn = None
            except (TimeoutException, NoSuchElementException):
                continue

        if not msg_btn:
            logger.info("❌ No Message button found - user may not be a 1st degree connection")
            return False

        # Click message button
        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", msg_btn)
            self.human_delay(1, 2)

            if not self.safe_click(driver, msg_btn):
                ActionChains(driver).move_to_element(msg_btn).click().perform()

            logger.info("✅ Message button clicked successfully")
            self.human_delay(2, 3)
        except Exception as e:
            logger.error(f"❌ Failed to click Message button: {e}")
            return False

        # Enhanced message composition
        compose_selectors = [
            ("css", ".msg-form__contenteditable"),
            ("css", "[data-test-id='message-composer-input']"),
            ("css", "div[role='textbox'][contenteditable='true']"),
            ("xpath", "//textarea[@aria-label='Write a message…']"),
            ("css", "div[contenteditable='true'][role='textbox']")
        ]

        compose_box = None
        for selector_type, selector in compose_selectors:
            try:
                if selector_type == "xpath":
                    compose_box = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    compose_box = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )

                if compose_box:
                    logger.info(f"✅ Message compose area found using: {selector}")
                    break
            except (TimeoutException, NoSuchElementException):
                continue

        if not compose_box:
            logger.error("❌ Could not find message compose area")
            return False

        # Type the message
        try:
            compose_box.click()
            self.human_delay(0.5, 1)
            compose_box.clear()

            # Type message character by character
            for char in message:
                compose_box.send_keys(char)
                time.sleep(random.uniform(0.05, 0.15))

            logger.info("✅ Message typed successfully")
            self.human_delay(1, 2)
        except Exception as e:
            logger.error(f"❌ Failed to type message: {e}")
            return False

        # Send the message
        send_button_selectors = [
            ("css", "button.msg-form__send-button[type='submit']"),
            ("css", "button[data-control-name='send_message']"),
            ("xpath", "//button[@type='submit' and .//span[text()='Send']]"),
            ("xpath", "//button[contains(@aria-label, 'Send') and @type='submit']"),
            ("css", "button[aria-label*='Send message']")
        ]

        send_btn = None
        for selector_type, selector in send_button_selectors:
            try:
                if selector_type == "xpath":
                    send_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    send_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )

                if send_btn and send_btn.is_enabled():
                    logger.info(f"✅ Send button found using: {selector}")
                    break
            except (TimeoutException, NoSuchElementException):
                continue

        if not send_btn or not send_btn.is_enabled():
            logger.error("❌ Send button not found or not enabled")
            return False

        try:
            if self.safe_click(driver, send_btn):
                logger.info(f"🎉 Message sent successfully to {name}!")
                self.human_delay(1, 2)
                return True
            else:
                logger.error("❌ Failed to click Send button")
                return False
        except Exception as e:
            logger.error(f"❌ Error sending message: {e}")
            return False

    def send_message_with_priority(self, driver, message, name, company):
        """Send message using priority order: connection with note -> connection without note -> direct message"""
        logger.info(f"🚀 Starting outreach process for {name} at {company}")

        try:
            # Wait for page to load completely
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.common.exceptions import TimeoutException
            
            WebDriverWait(driver, 15).until(
                lambda d: d.execute_script('return document.readyState') == 'complete'
            )
            self.human_delay(2, 4)
        except TimeoutException:
            logger.warning("⚠️ Page load timeout - proceeding anyway")

        # Extract profile data for better personalization
        profile_data = self.extract_profile_data(driver)

        # PRIORITY 1: Try connection request with note
        logger.info("🎯 Priority 1: Attempting connection request with personalized note...")
        if self.send_connection_request_with_note(driver, message, name):
            logger.info(f"✅ Successfully sent connection request with note to {name}")
            return True

        # PRIORITY 2: Try connection request without note
        logger.info("🎯 Priority 2: Attempting connection request without note...")
        if self.send_connection_request_without_note(driver, name):
            logger.info(f"✅ Successfully sent connection request without note to {name}")
            return True

        # PRIORITY 3: Try direct message
        logger.info("🎯 Priority 3: Attempting direct message...")
        if self.send_direct_message(driver, message, name):
            logger.info(f"✅ Successfully sent direct message to {name}")
            return True

        # If all methods fail
        logger.error(f"❌ All outreach methods failed for {name}")
        return False

    def search_and_connect(self, driver, keywords, max_invites=20):
        """Search for profiles and send connection requests"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException
        from urllib.parse import quote_plus
        
        logger.info(f"🔍 Searching for: {keywords}")
        url = (f"https://www.linkedin.com/search/results/people/"
               f"?keywords={quote_plus(keywords)}&origin=GLOBAL_SEARCH_HEADER")
        
        driver.get(url)
        time.sleep(3)
        
        sent_count = 0
        page_loops = 0
        total_attempts = 0
        
        while sent_count < max_invites and page_loops < 10:
            logger.info(f"📊 Current status: {sent_count}/{max_invites} invitations sent")
            
            # Find connect buttons
            connect_buttons = self.find_connect_buttons_enhanced(driver)
            
            if not connect_buttons:
                logger.info("No connect buttons found on this page")
                if not self.go_to_next_page(driver):
                    break
                page_loops += 1
                continue
            
            for btn in connect_buttons:
                if sent_count >= max_invites:
                    logger.info(f"🎯 Target reached: {sent_count}/{max_invites} invitations sent")
                    return sent_count
                
                total_attempts += 1
                logger.info(f"🔄 Attempting connection #{total_attempts}")
                
                try:
                    if self.click_connect_and_validate(driver, btn):
                        sent_count += 1
                        logger.info(f"✅ Success! Sent invitation #{sent_count}/{max_invites}")
                        time.sleep(random.uniform(2, 4))
                    else:
                        logger.info(f"❌ Failed to send invitation (attempt #{total_attempts})")
                except Exception as e:
                    logger.debug(f"Exception during connection attempt: {e}")
                    continue
            
            # Navigate to next page
            if not self.go_to_next_page(driver):
                logger.info("No more pages available")
                break
            page_loops += 1
            time.sleep(random.uniform(1, 3))
        
        logger.info(f"🏁 Final results: {sent_count}/{max_invites} invitations sent ({total_attempts} total attempts)")
        return sent_count

    def find_connect_buttons_enhanced(self, driver):
        """Find connect buttons with enhanced detection"""
        from selenium.webdriver.common.by import By
        
        selectors = [
            "//button[contains(text(), 'Connect') and not(contains(@class, 'artdeco-button--disabled'))]",
            "//button[.//span[text()='Connect'] and not(contains(@class, 'disabled'))]",
            "//button[contains(@aria-label, 'Connect') and not(@disabled)]"
        ]
        
        buttons = []
        for selector in selectors:
            try:
                found_buttons = driver.find_elements(By.XPATH, selector)
                for btn in found_buttons:
                    if btn.is_displayed() and btn.is_enabled():
                        buttons.append(btn)
            except Exception as e:
                logger.debug(f"Selector failed: {selector}, Error: {e}")
        
        unique_buttons = list(dict.fromkeys(buttons))
        logger.info(f"Found {len(unique_buttons)} available connect buttons")
        return unique_buttons

    def click_connect_and_validate(self, driver, button):
        """Click connect button and validate success"""
        driver.execute_script("arguments[0].scrollIntoView(true);", button)
        driver.execute_script("arguments[0].click();", button)
        time.sleep(1)
        return self.handle_connect_modal(driver)

    def handle_connect_modal(self, driver):
        """Handle connection modal and send invitation"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException
        
        # Click send button
        for xpath in [
            "//button[normalize-space()='Send without a note']",
            "//button[normalize-space()='Send now']",
            "//button[contains(@aria-label,'Send')]"
        ]:
            try:
                btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                btn.click()
                break
            except TimeoutException:
                continue
        
        # Wait for success confirmation
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//button[normalize-space()='Pending']"))
            )
            return True
        except TimeoutException:
            return False

    def go_to_next_page(self, driver):
        """Navigate to next page of search results"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException
        
        try:
            wait = WebDriverWait(driver, 5)
            next_button = wait.until(EC.element_to_be_clickable((
                By.XPATH,
                "//button[@aria-label='Next' and not(@disabled)] | //a[@aria-label='Next']"
            )))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", next_button)
            next_button.click()
            return True
        except TimeoutException:
            return False
        except Exception as e:
            return False

    def navigate_to_messaging(self, driver):
        """Navigate to LinkedIn messaging"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        logger.info("Navigating to LinkedIn messaging...")
        try:
            driver.get("https://www.linkedin.com/messaging")
            WebDriverWait(driver, 15).until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "ul.msg-conversations-container__conversations-list")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-threads"))
                )
            )
            logger.info("Successfully loaded messaging page.")
            self.human_delay(2, 3)
            return True
        except:
            logger.error("Failed to load messaging page in time.")
            return False

    def process_inbox_replies_enhanced(self, driver, max_replies=5):
        """Process unread messages with improved reliability"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        logger.info("🤖 Starting AI inbox processing...")
        results = []
        
        if not self.navigate_to_messaging(driver):
            return {"success": False, "error": "Messaging navigation failed"}
        
        try:
            # Find unread conversations
            unread_selector = (
                "li.msg-conversations-container__conversation-list-item:has(.notification-badge--show), "
                "li.conversation-list-item:has(.unread)"
            )
            
            unread_items = WebDriverWait(driver, 10).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, unread_selector))
            )
            
            logger.info(f"Found {len(unread_items)} unread conversations")
            
            for idx, item in enumerate(unread_items[:max_replies]):
                try:
                    # Extract participant name
                    name_elem = item.find_element(
                        By.CSS_SELECTOR,
                        ".msg-conversation-listitem__participant-names, .conversation-list-item__participant-names"
                    )
                    name = name_elem.text.strip()
                    
                    logger.info(f"Processing conversation with {name} ({idx+1}/{len(unread_items)})")
                    
                    # Open conversation
                    driver.execute_script("arguments[0].click();", item)
                    self.human_delay(2, 3)
                    
                    # Wait for conversation to load
                    WebDriverWait(driver, 10).until(
                        EC.any_of(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-s-message-list-content")),
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-thread"))
                        )
                    )
                    
                    # Get message history
                    history = self.get_conversation_history(driver)
                    if not history:
                        logger.warning("No messages found, skipping")
                        results.append({"name": name, "status": "skipped", "reason": "empty history"})
                        self.navigate_to_messaging(driver)
                        continue
                    
                    # Check if last message is from user
                    if history and history[-1]["sender"] == "You":
                        logger.info("Last message was from user, skipping")
                        results.append({"name": name, "status": "skipped", "reason": "already replied"})
                        self.navigate_to_messaging(driver)
                        continue
                    
                    # Generate AI response
                    ai_reply = self.generate_ai_chat_response(history)
                    
                    # Send response
                    if self.send_chat_message(driver, ai_reply):
                        logger.info(f"✅ Replied to {name}")
                        results.append({"name": name, "status": "replied", "message": ai_reply})
                    else:
                        logger.error(f"❌ Failed to reply to {name}")
                        results.append({"name": name, "status": "failed", "reason": "send error"})
                    
                    # Return to inbox
                    self.navigate_to_messaging(driver)
                    self.human_delay(2, 4)
                    
                except Exception as e:
                    logger.error(f"Error processing conversation: {e}")
                    results.append({"name": f"Unknown{idx}", "status": "error", "reason": str(e)})
                    try:
                        self.navigate_to_messaging(driver)
                    except:
                        driver.refresh()
            
            return {"success": True, "results": results}
            
        except Exception as e:
            logger.error(f"Inbox processing failed: {e}")
            return {"success": False, "error": str(e)}

    def get_conversation_history(self, driver):
        """Extract conversation history"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        logger.info("Extracting conversation history...")
        conversation = []
        
        try:
            # Wait for message container
            self.wait.until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-s-message-list-content")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-thread"))
                )
            )
            
            # New UI extraction
            if driver.find_elements(By.CSS_SELECTOR, "div.msg-s-message-list-content"):
                message_elements = driver.find_elements(By.CSS_SELECTOR, "li.msg-s-message-list__event")
                
                for msg in message_elements:
                    try:
                        # Extract sender name
                        try:
                            sender = msg.find_element(By.CSS_SELECTOR, ".msg-s-message-group__name").text.strip()
                        except:
                            sender = "You"
                        
                        # Extract message text
                        try:
                            content = msg.find_element(By.CSS_SELECTOR, ".msg-s-event-listitem__body").text.strip()
                        except:
                            content = ""
                        
                        if content:
                            conversation.append({"sender": sender, "message": content})
                    except Exception as e:
                        logger.debug(f"Skipping message: {e}")
            
            logger.info(f"Extracted {len(conversation)} messages")
            return conversation
            
        except Exception as e:
            logger.error(f"History extraction failed: {e}")
            return []

    def generate_ai_chat_response(self, conversation_history, user_persona="a helpful professional assistant"):
        """Generate contextual response to a conversation using Gemini AI"""
        if not self.model:
            logger.error("AI model is not initialized. Cannot generate response.")
            return "Sorry, I am unable to generate a response at this time."
        
        if not conversation_history:
            logger.warning("Conversation history is empty. Cannot generate a contextual response.")
            return "Could you please provide more context?"
        
        logger.info("Generating AI response for the chat...")
        
        # Format the conversation history for the AI prompt
        formatted_history = "\n".join([f"{msg['sender']}: {msg['message']}" for msg in conversation_history])
        
        # Get the name of the other person
        other_person_name = "there"
        for msg in reversed(conversation_history):
            if msg['sender'] != 'You':
                other_person_name = msg['sender'].split()[0]  # Get first name
                break
        
        prompt = f"""Craft a professional LinkedIn reply based on this conversation. Guidelines:

1. Be concise (1-2 sentences max)
2. Match the sender's tone (formal/casual)
3. Address unread messages specifically
4. Never use markdown or special formatting
5. Respond naturally to questions
6. Sign with just your first name

Recent messages:
{formatted_history}

Response:"""
        
        try:
            response = self.model.generate_content(prompt)
            ai_message = response.text.strip()
            
            # Clean up any AI-added labels
            ai_message = re.sub(r'^(Your Response:|Response:)\s*', '', ai_message, flags=re.IGNORECASE)
            
            logger.info(f"AI generated response: {ai_message}")
            return ai_message
            
        except Exception as e:
            logger.error(f"AI response generation failed: {e}")
            return "I appreciate you reaching out. Let me review this and get back to you shortly."

    def send_chat_message(self, driver, message):
        """Types and sends a message in the currently active chat window"""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, NoSuchElementException
        
        logger.info(f"Sending message: '{message[:50]}...'")
        
        try:
            # Wait for message box to be ready
            message_box_selector = "div.msg-form__contenteditable[role='textbox']"
            message_box = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, message_box_selector))
            )
            
            # Wait for any previous messages to clear
            self.human_delay(1, 2)
            
            # Clear any existing text
            driver.execute_script("arguments[0].innerText = '';", message_box)
            message_box.send_keys(" ")  # Trigger any required events
            self.human_delay(0.5, 1)
            
            # Type message
            self.type_like_human(message_box, message)
            self.human_delay(1, 2)
            
            # Find and click the send button
            send_button = driver.find_element(
                By.CSS_SELECTOR,
                "button.msg-form__send-button[type='submit'], button.msg-form-send-button"
            )
            
            # Ensure button is enabled
            if send_button.is_enabled():
                self.safe_click(driver, send_button)
                logger.info("Message sent successfully.")
                self.human_delay(2, 4)
                return True
            else:
                logger.error("Send button is disabled.")
                return False
                
        except TimeoutException:
            logger.error("Message input box not found or not interactable.")
            return False
        except NoSuchElementException:
            logger.error("Send button not found.")
            return False
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    # ==============================================
    # ENHANCED CAMPAIGN RUNNERS
    # ==============================================

    def run_enhanced_outreach_campaign(self, campaign_id, user_config, campaign_data):
        """Run outreach campaign with PROPER message generation and user confirmation"""
        try:
            # Initialize campaign status
            self.active_campaigns[campaign_id] = {
                'status': 'initializing',
                'progress': 0,
                'total': campaign_data.get('max_contacts', 0),
                'successful': 0,
                'failed': 0,
                'skipped': 0,
                'already_messaged': 0,
                'stop_requested': False,
                'awaiting_confirmation': False,
                'current_contact': None,
                'start_time': datetime.now().isoformat(),
                'contacts_processed': [],
                'user_action': None
            }

            # Initialize LinkedIn automation
            automation = LinkedInAutomation(
                email=user_config.get('linkedin_email', self.config['linkedin_email']),
                password=user_config.get('linkedin_password', self.config['linkedin_password']),
                api_key=user_config.get('gemini_api_key', self.config['gemini_api_key'])
            )

            # Login to LinkedIn - USE ORIGINAL PERSISTENT SESSION APPROACH
            self.active_campaigns[campaign_id]['status'] = 'logging_in'
            if not automation.login():
                self.active_campaigns[campaign_id]['status'] = 'failed'
                self.active_campaigns[campaign_id]['error'] = 'LinkedIn login failed'
                automation.close()
                return

            self.active_campaigns[campaign_id]['status'] = 'running'

            # Load tracked profiles
            tracked_profiles = set()
            tracked_profiles_file = 'messaged_profiles.json'
            if os.path.exists(tracked_profiles_file):
                try:
                    with open(tracked_profiles_file, 'r', encoding='utf-8') as f:
                        tracked_profiles = set(json.load(f))
                except:
                    pass

            # Process contacts with MESSAGE GENERATION AND USER CONFIRMATION
            contacts = campaign_data.get('contacts', [])[:campaign_data.get('max_contacts', 20)]
            
            for idx, contact in enumerate(contacts):
                if self.active_campaigns[campaign_id]['stop_requested']:
                    self.active_campaigns[campaign_id]['status'] = 'stopped'
                    break

                try:
                    linkedin_url = contact.get('LinkedIn_profile', '')
                    if not linkedin_url or 'linkedin.com/in/' not in linkedin_url:
                        self.active_campaigns[campaign_id]['failed'] += 1
                        continue

                    # Check if already messaged
                    if linkedin_url in tracked_profiles:
                        logger.info(f"⏭️ Skipping {contact['Name']} - already messaged")
                        self.active_campaigns[campaign_id]['already_messaged'] += 1
                        self.active_campaigns[campaign_id]['progress'] += 1
                        continue

                    # Navigate to profile
                    logger.info(f"🌐 Navigating to {contact['Name']}'s profile...")
                    automation.driver.get(linkedin_url)
                    time.sleep(3)

                    # Extract profile data
                    profile_data = automation.extract_profile_data()

                    # 🚀 GENERATE PERSONALIZED MESSAGE FIRST
                    logger.info(f"🤖 Generating personalized message for {contact['Name']}...")
                    message = automation.generate_message(
                        contact['Name'],
                        contact['Company'],
                        contact['Role'],
                        contact.get('services and products_1', ''),
                        contact.get('services and products_2', ''),
                        profile_data
                    )

                    # 📝 SET UP USER CONFIRMATION
                    self.active_campaigns[campaign_id]['current_contact'] = {
                        'contact': contact,
                        'message': message,
                        'contact_index': idx,
                        'profile_data': profile_data
                    }

                    self.active_campaigns[campaign_id]['awaiting_confirmation'] = True
                    self.active_campaigns[campaign_id]['status'] = 'awaiting_user_action'

                    logger.info(f"⏳ Waiting for user confirmation for {contact['Name']}")
                    logger.info(f"💬 Generated message: {message}")

                    # Notify dashboard about the preview
                    self.report_progress_to_dashboard(campaign_id)

                    # ⏰ WAIT FOR USER DECISION WITH TIMEOUT
                    timeout_count = 0
                    max_timeout = 300  # 5 minutes
                    
                    while (self.active_campaigns[campaign_id]['awaiting_confirmation'] and
                        timeout_count < max_timeout and
                        not self.active_campaigns[campaign_id]['stop_requested']):
                        time.sleep(1)
                        timeout_count += 1

                    # Check if user made a decision
                    user_action = self.active_campaigns[campaign_id].get('user_action')
                    if user_action:
                        action = user_action.get('action')
                        custom_message = user_action.get('message', message)

                        if action == 'skip':
                            logger.info(f"⏭️ User chose to skip {contact['Name']}")
                            self.active_campaigns[campaign_id]['skipped'] += 1
                            self.active_campaigns[campaign_id]['progress'] += 1
                            # Reset states
                            self.active_campaigns[campaign_id]['user_action'] = None
                            self.active_campaigns[campaign_id]['awaiting_confirmation'] = False
                            self.active_campaigns[campaign_id]['current_contact'] = None
                            self.active_campaigns[campaign_id]['status'] = 'running'
                            continue

                        elif action == 'send':
                            logger.info(f"✅ User confirmed sending to {contact['Name']}")
                            message = custom_message  # Use edited message if provided

                    # Check for timeout or stop
                    if timeout_count >= max_timeout:
                        logger.warning(f"⏰ Timeout waiting for user decision on {contact['Name']}")
                        self.active_campaigns[campaign_id]['skipped'] += 1
                        self.active_campaigns[campaign_id]['progress'] += 1
                        continue

                    if self.active_campaigns[campaign_id]['stop_requested']:
                        break

                    # Reset confirmation state
                    self.active_campaigns[campaign_id]['awaiting_confirmation'] = False
                    self.active_campaigns[campaign_id]['current_contact'] = None
                    self.active_campaigns[campaign_id]['status'] = 'running'
                    self.active_campaigns[campaign_id]['user_action'] = None

                    # 🎯 NOW USE THE 3-TIER PRIORITY APPROACH FROM LINKEDIN_AUTOMATION_SCRIPT
                    logger.info(f"🚀 Starting outreach process for {contact['Name']} at {contact['Company']}")
                    
                    success = False
                    
                    # PRIORITY 1: Try connection request with note
                    logger.info("🎯 Priority 1: Attempting connection request with personalized note...")
                    success = automation.send_connection_request_with_note(message, contact['Name'])
                    
                    if not success:
                        # PRIORITY 2: Try connection request without note  
                        logger.info("🎯 Priority 2: Attempting connection request without note...")
                        success = automation.send_connection_request_without_note(contact['Name'])
                    
                    if not success:
                        # PRIORITY 3: Try direct message
                        logger.info("🎯 Priority 3: Attempting direct message...")
                        success = automation.send_direct_message(message, contact['Name'])

                    # Record results
                    contact_result = {
                        'name': contact['Name'],
                        'company': contact['Company'],
                        'role': contact['Role'],
                        'linkedin_url': linkedin_url,
                        'message': message,
                        'success': success,
                        'timestamp': datetime.now().isoformat()
                    }

                    if success:
                        self.active_campaigns[campaign_id]['successful'] += 1
                        # Add to tracked profiles
                        tracked_profiles.add(linkedin_url)
                        try:
                            with open(tracked_profiles_file, 'w', encoding='utf-8') as f:
                                json.dump(list(tracked_profiles), f, ensure_ascii=False, indent=2)
                        except:
                            pass
                        logger.info(f"✅ Successfully connected with {contact['Name']}")
                        time.sleep(random.uniform(60, 120))  # Delay between successful connections
                    else:
                        self.active_campaigns[campaign_id]['failed'] += 1
                        logger.error(f"❌ Failed to connect with {contact['Name']}")

                    self.active_campaigns[campaign_id]['contacts_processed'].append(contact_result)
                    self.active_campaigns[campaign_id]['progress'] += 1

                    # Report progress to dashboard
                    self.report_progress_to_dashboard(campaign_id)

                except Exception as e:
                    logger.error(f"❌ Error processing {contact.get('Name', 'Unknown')}: {e}")
                    self.active_campaigns[campaign_id]['failed'] += 1
                    self.active_campaigns[campaign_id]['progress'] += 1

            # Campaign completed
            self.active_campaigns[campaign_id]['status'] = 'completed'
            self.active_campaigns[campaign_id]['end_time'] = datetime.now().isoformat()

            # Final progress report
            self.report_progress_to_dashboard(campaign_id, final=True)
            automation.close()

        except Exception as e:
            logger.error(f"❌ Campaign {campaign_id} error: {e}")
            self.active_campaigns[campaign_id]['status'] = 'failed'
            self.active_campaigns[campaign_id]['error'] = str(e)


    def run_enhanced_keyword_search(self, search_id, user_config, search_params):
        """Run keyword-based LinkedIn search and connect with enhanced functionality"""
        try:
            # Initialize browser
            driver = self.initialize_browser()
            
            # Login
            if not self.linkedin_login(
                driver,
                user_config.get('linkedin_email', self.config['linkedin_email']),
                user_config.get('linkedin_password', self.config['linkedin_password'])
            ):
                logger.error("❌ LinkedIn login failed for keyword search")
                driver.quit()
                return

            # Perform search
            keywords = search_params.get('keywords', '')
            max_invites = search_params.get('max_invites', 10)
            search_type = search_params.get('search_type', 'search_only')
            
            logger.info(f"🔍 Starting keyword search for: {keywords}")

            if search_type == 'search_and_connect':
                results = self.search_and_connect(driver, keywords, max_invites)
            else:
                # Just search for profiles without connecting
                results = {'profiles_found': [], 'search_completed': True}

            # Report results to dashboard
            self.report_search_results_to_dashboard(search_id, {
                'keywords': keywords,
                'results': results,
                'search_type': search_type,
                'timestamp': datetime.now().isoformat()
            })

            driver.quit()

        except Exception as e:
            logger.error(f"❌ Keyword search {search_id} error: {e}")

    def run_enhanced_inbox_processing(self, process_id, user_config):
        """Process LinkedIn inbox with AI responses using enhanced functionality"""
        try:
            # Initialize browser
            driver = self.initialize_browser()
            
            # Login
            if not self.linkedin_login(
                driver,
                user_config.get('linkedin_email', self.config['linkedin_email']),
                user_config.get('linkedin_password', self.config['linkedin_password'])
            ):
                logger.error("❌ LinkedIn login failed for inbox processing")
                driver.quit()
                return

            # Process inbox
            logger.info("📬 Starting enhanced inbox processing")
            results = self.process_inbox_replies_enhanced(driver)

            # Report results to dashboard
            self.report_inbox_results_to_dashboard(process_id, results)

            driver.quit()

        except Exception as e:
            logger.error(f"❌ Inbox processing {process_id} error: {e}")

    def report_progress_to_dashboard(self, campaign_id, final=False):
        """Report campaign progress back to dashboard with better error handling"""
        try:
            dashboard_url = self.config.get('dashboard_url')
            if not dashboard_url:
                logger.debug("No dashboard URL configured")
                return

            progress_data = self.active_campaigns.get(campaign_id, {})
            
            # Include current contact info if awaiting confirmation
            if progress_data.get('awaiting_confirmation') and progress_data.get('current_contact'):
                progress_data['awaiting_action'] = True
                progress_data['current_contact_preview'] = progress_data['current_contact']
            
            endpoint = f"{dashboard_url}/api/campaign_progress"
            
            response = requests.post(endpoint, json={
                'campaign_id': campaign_id,
                'progress': progress_data,
                'final': final
            }, timeout=30, verify=True)  # Increased timeout and enable SSL verification
            
            if response.status_code == 200:
                logger.debug(f"✅ Successfully reported progress for campaign {campaign_id}")
            else:
                logger.warning(f"⚠️ Dashboard progress report returned status {response.status_code}")

        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout reporting progress to dashboard for campaign {campaign_id}")
        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠️ Connection error reporting progress to dashboard for campaign {campaign_id}")
        except Exception as e:
            logger.debug(f"Could not report progress for campaign {campaign_id}: {e}")

    def report_search_results_to_dashboard(self, search_id, results):
        """Report search results back to dashboard with better error handling"""
        try:
            dashboard_url = self.config.get('dashboard_url')
            if not dashboard_url:
                return

            endpoint = f"{dashboard_url}/api/search_results"
            
            response = requests.post(endpoint, json={
                'search_id': search_id,
                'results': results
            }, timeout=30, verify=True)
            
            if response.status_code == 200:
                logger.info(f"✅ Successfully reported search results for {search_id}")
            else:
                logger.warning(f"⚠️ Dashboard search report returned status {response.status_code}")

        except Exception as e:
            logger.debug(f"Could not report search results for {search_id}: {e}")

    def report_inbox_results_to_dashboard(self, process_id, results):
        """Report inbox processing results back to dashboard with better error handling"""
        try:
            dashboard_url = self.config.get('dashboard_url')
            if not dashboard_url:
                return

            endpoint = f"{dashboard_url}/api/inbox_results"
            
            response = requests.post(endpoint, json={
                'process_id': process_id,
                'results': results
            }, timeout=30, verify=True)
            
            if response.status_code == 200:
                logger.info(f"✅ Successfully reported inbox results for {process_id}")
            else:
                logger.warning(f"⚠️ Dashboard inbox report returned status {response.status_code}")

        except Exception as e:
            logger.debug(f"Could not report inbox results for {process_id}: {e}")

    def start_client(self):
        """Start the client application"""
        self.running = True

        # Start Flask server in background thread
        self.flask_thread = threading.Thread(
            target=self._run_flask_app,
            daemon=True
        )
        self.flask_thread.start()

        # Show status GUI
        self.show_status_gui()

    def _run_flask_app(self):
        """Run Flask app"""
        try:
            self.flask_app.run(
                host='127.0.0.1',
                port=self.config['local_port'],
                debug=False,
                use_reloader=False
            )
        except Exception as e:
            logger.error(f"❌ Flask app error: {e}")

    def show_status_gui(self):
        """Show client status GUI"""
        sg.theme('DarkBlue3')

        layout = [
            [sg.Text('LinkedIn Automation Client', font=('Helvetica', 16, 'bold'))],
            [sg.Text(f'Status: Running on port {self.config["local_port"]}', key='status')],
            [sg.Text(f'Dashboard: {self.config["dashboard_url"]}', key='dashboard')],
            [sg.Text('')],
            [sg.Text('Active Campaigns:', font=('Helvetica', 12, 'bold'))],
            [sg.Multiline('No active campaigns', key='campaigns', size=(80, 15), disabled=True)],
            [sg.Text('')],
            [sg.Button('Refresh', size=(10,1)), sg.Button('Stop Client', size=(10,1))]
        ]

        window = sg.Window('LinkedIn Automation Client - Enhanced Status', layout, finalize=True)

        while self.running:
            event, values = window.read(timeout=3000)  # 3 second timeout

            if event in (sg.WIN_CLOSED, 'Stop Client'):
                self.running = False
                break

            if event == 'Refresh' or event == sg.TIMEOUT_EVENT:
                # Update campaigns display
                campaigns_text = ""
                if self.active_campaigns:
                    for cid, status in self.active_campaigns.items():
                        campaigns_text += f"Campaign {cid[:8]}...\n"
                        campaigns_text += f"  Status: {status.get('status', 'unknown')}\n"
                        campaigns_text += f"  Progress: {status.get('progress', 0)}/{status.get('total', 0)}\n"
                        campaigns_text += f"  Success: {status.get('successful', 0)}, Failed: {status.get('failed', 0)}\n"
                        campaigns_text += f"  Skipped: {status.get('skipped', 0)}, Already Messaged: {status.get('already_messaged', 0)}\n"
                        
                        if status.get('awaiting_confirmation'):
                            campaigns_text += f"  ⏳ AWAITING USER CONFIRMATION\n"
                            current_contact = status.get('current_contact', {}).get('contact', {})
                            campaigns_text += f"  Contact: {current_contact.get('Name', 'Unknown')}\n"
                        
                        campaigns_text += "\n"
                else:
                    campaigns_text = "No active campaigns"

                window['campaigns'].update(campaigns_text)

        window.close()
        logger.info("👋 Client application stopped")

    def cleanup(self):
        """Cleanup resources"""
        self.running = False

        # Close any active automation instances
        for automation in self.automation_instances.values():
            try:
                automation.close()
            except:
                pass

def signal_handler(signum, frame):
    """Handle system signals for graceful shutdown"""
    logger.info("🛑 Received shutdown signal")
    sys.exit(0)

def main():
    """Main function"""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Create and start client
        client = EnhancedLinkedInAutomationClient()

        # Register cleanup function
        atexit.register(client.cleanup)

        logger.info("🚀 Starting Enhanced LinkedIn Automation Client")
        client.start_client()

    except KeyboardInterrupt:
        logger.info("👋 Client stopped by user")
    except Exception as e:
        logger.error(f"❌ Client error: {e}")
        sg.popup_error(f"Client Error: {e}")
    finally:
        logger.info("🔚 Client application terminated")

if __name__ == "__main__":
    main()
