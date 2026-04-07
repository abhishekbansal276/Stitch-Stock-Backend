import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.services.firebase import db

class EmailService:
    def __init__(self):
        # SMTP Configuration (Load from Environment)
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_username = os.getenv("SMTP_USERNAME")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.sender_email = os.getenv("SMTP_FROM_EMAIL", self.smtp_username)

    def _send_email(self, recipient: str, subject: str, body: str):
        """Standard SMTP delivery engine."""
        if not all([self.smtp_username, self.smtp_password, recipient]):
            print("--- SMTP SKIP: Credentials or Recipient Missing ---")
            return False

        try:
            msg = MIMEMultipart()
            msg['From'] = f"Stitch Stock Alerts <{self.sender_email}>"
            msg['To'] = recipient
            msg['Subject'] = subject
            
            msg.attach(MIMEText(body, 'plain'))
            
            # Context for secure SMTP
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.smtp_username, self.smtp_password)
            server.send_message(msg)
            server.quit()
            
            print(f"--- EMAIL DISPATCHED TO {recipient} ---")
            return True
        except Exception as e:
            print(f"--- SMTP ERROR: {e} ---")
            return False

    def send_low_stock_alert(self, item_name, product_code, current_qty, min_level, unit):
        """Triggered when stock dips below the Admin threshold."""
        subject = f"⚠️ LOW STOCK ALERT: {item_name} ({product_code})"
        
        body = (
            f"Inventory Alert for Stitch Stock System\n"
            f"----------------------------------------\n\n"
            f"Product: {item_name}\n"
            f"Code: {product_code}\n"
            f"Current Balance: {current_qty} {unit}\n"
            f"Minimum Threshold: {min_level} {unit}\n\n"
            f"This item has dipped below the minimum stock level set by the administrator. "
            f"Please review procurement or relocation requirements immediately.\n\n"
            f"-- Stitch Stock Automated Guard"
        )
        
        # Dynamic Fetch: Get all recipients from Firestore
        try:
            doc = db.collection('alert_config').document('low_stock').get()
            if not doc.exists:
                print("--- ALERT SKIP: alert_config/low_stock doc not found ---")
                return False
                 
            recipients = doc.to_dict().get('emails', [])
            if not recipients:
                print("--- ALERT SKIP: No recipients defined in Firestore ---")
                return False
                
            success = True
            for email in recipients:
                if not self._send_email(email, subject, body):
                    success = False
            return success
        except Exception as e:
            print(f"--- RECIPIENT FETCH ERROR: {e} ---")
            return False

email_service = EmailService()
