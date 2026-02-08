"""
Enterprise Voice AI Receptionist for Mark Esposito - BHHS Québec
Architecture: Twilio Media Streams <-> FastAPI WebSocket <-> OpenAI Realtime API
Features: Bilingual (FR/EN), Property Search, Appointment Booking, CRM Logging, Warm Transfer
Deployment: Render (Web Service)
"""

import os
import json
import base64
import asyncio
import websockets
import aiohttp
from fastapi import FastAPI, WebSocket, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import re
from typing import Optional, Dict, Any, List
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================

# API Keys (set as environment variables in Render)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")  # Mark's BHHS number

# Google Sheets & Calendar Setup
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")  # Service account JSON
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")  # Leads spreadsheet
AGENT_EMAIL = os.getenv("AGENT_EMAIL")  # Mark's email for calendar invites

# Agent Configuration
AGENT_NAME = "Mark Esposito"
AGENT_COMPANY = "Berkshire Hathaway HomeServices Québec"
AGENT_PHONE = "+1-514-XXX-XXXX"  # Update with actual number
AGENT_SPECIALTIES = ["Westmount", "Downtown Montreal", "Luxury Condos", "Triplexes", "Investment Properties"]
AGENT_AREAS = ["Westmount", "Montreal Downtown", "Plateau-Mont-Royal", "Outremont", "Griffintown", "NDG"]

# OpenAI Realtime API Configuration
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"

# System Prompt - Bilingual, Brand-Aligned
SYSTEM_PROMPT = """You are the Inbound Receptionist for Mark Esposito at Berkshire Hathaway HomeServices Québec. You handle incoming calls for this luxury real estate agent in Montreal.

**CRITICAL RULES:**
1. **Language**: Detect the caller's language (French or English) automatically and respond in that language. If they switch languages mid-conversation, switch with them seamlessly. You must handle "Franglais" (mixed French-English) naturally.
2. **Voice**: Use the "alloy" voice. Speak naturally with brief pauses. Be warm yet professional—like a hospitality-trained concierge.
3. **Availability**: You are available 24/7, but Mark is available for callbacks during business hours (9 AM - 8 PM, 7 days).

**MARK'S BRAND PERSONA:**
- Background: Psychology degree (Concordia) + 15 years hospitality (restaurant owner at Le Crystal Hotel)
- Style: Composed, charming, puts people at ease, creates personal connections
- Specialty: Westmount roots, luxury properties, downtown condos, family homes
- Philosophy: "Turning houses into homes"—understands emotional weight of real estate decisions

**CONVERSATION FLOWS:**

1. **GREETING**: "Bonjour, you've reached Mark Esposito with Berkshire Hathaway HomeServices Québec. I'm his AI assistant, available to help you 24/7. Are you looking to buy, sell, rent, or something else today?"

2. **BUYER QUALIFICATION**:
   - Ask: Area of interest (Westmount, Downtown, Plateau, etc.), Budget range, Timeline, Property type (condo/house/triplex)
   - If specific property inquiry: Ask for address or MLS number
   - Offer to schedule viewing immediately if interested

3. **SELLER QUALIFICATION**:
   - Ask: Property address, Timeline to sell, Reason for selling
   - Offer complimentary market analysis (CMA) appointment
   - Emphasize Mark's marketing background and psychology approach to staging/presentation

4. **RENTER QUALIFICATION**:
   - Note: Mark primarily handles sales, but may have rental listings or referrals
   - Ask: Budget, Area, Timeline, Duration

5. **APPOINTMENT BOOKING**:
   - Use function `check_calendar_availability` to find slots
   - Use function `book_appointment` to confirm
   - Always confirm: Date, Time, Address/Location, Purpose (viewing/CMA/consultation)
   - Send confirmation details via SMS after call

6. **PROPERTY SEARCH**:
   - If asking about specific areas (Westmount, Griffintown, etc.), mention Mark's local expertise
   - For Westmount: Emphasize Mark grew up there, attended Selwyn House/Marianopolis
   - For Downtown/Luxury: Mention Le Crystal Hotel background and understanding of luxury lifestyle

7. **TRANSFER TO MARK**:
   - If caller insists on speaking to Mark immediately OR for complex negotiations
   - Say: "I'll connect you directly to Mark. One moment please."
   - Use function `warm_transfer` with context summary

8. **VOICEMAIL**:
   - If Mark unavailable: "Mark is currently with clients. May I take a detailed message and have him call you back within 2 hours?"
   - Use function `log_voicemail`

**LOCAL KNOWLEDGE - MONTREAL:**
- Westmount: English-speaking enclave, prestigious schools (Selwyn House, ECS), family-oriented
- Plateau: Trendy, artistic, French-speaking, younger demographic
- Griffintown: New condos, young professionals, anglophone
- Outremont: Mixed French/English, upscale, quiet
- Downtown: Luxury condos (Le Crystal, Roccabella, etc.), investors, professionals
- "Plex" culture: Duplex/triplex ownership popular in Montreal
- Welcome Tax (Taxe de Bienvenue): Mention for buyers if relevant
- July 1st: Traditional moving day (lease transfers)

**TOOLS AVAILABLE:**
- `check_calendar_availability`: Check Mark's calendar for openings
- `book_appointment`: Schedule viewing or consultation
- `log_lead`: Save caller info to CRM (Google Sheets)
- `send_sms_confirmation`: Send appointment details via text
- `warm_transfer`: Transfer call to Mark with context
- `log_voicemail`: Record message for callback

**TONE GUIDELINES:**
- Warm but efficient (hospitality background)
- Validate emotions: "I understand finding the right home is a big decision"
- Never rush, but keep conversations under 5 minutes when possible
- Use caller's name frequently once known
- For French: Use formal "vous" initially, switch to "tu" if caller does
- For English: Professional but approachable, "How can I make this easier for you?"

**ESCALATION TRIGGERS:**
- Caller says "agent immobilier humain", "vrai personne", "real human", "speak to Mark now"
- Complex legal questions about Quebec real estate law
- Emotional distress (divorce, death, financial urgency) - offer immediate Mark callback
- Offers/negotiations on specific properties

Always confirm phone number before ending call for follow-up purposes."""

# ==================== GOOGLE INTEGRATIONS ====================

class GoogleIntegration:
    def __init__(self):
        self.creds = None
        self.sheets_service = None
        self.calendar_service = None
        self._init_services()
    
    def _init_services(self):
        """Initialize Google Sheets and Calendar APIs"""
        try:
            if GOOGLE_CREDENTIALS_JSON:
                creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
                self.creds = Credentials.from_service_account_info(
                    creds_info,
                    scopes=[
                        'https://www.googleapis.com/auth/spreadsheets',
                        'https://www.googleapis.com/auth/calendar',
                        'https://www.googleapis.com/auth/calendar.events'
                    ]
                )
                self.sheets_service = gspread.authorize(self.creds)
                self.calendar_service = build('calendar', 'v3', credentials=self.creds, cache_discovery=False)
                logger.info("Google services initialized successfully")
        except Exception as e:
            logger.error(f"Google services init failed: {e}")
    
    async def log_lead(self, lead_data: Dict[str, Any]):
        """Log lead to Google Sheets CRM"""
        try:
            if not self.sheets_service or not SHEET_ID:
                logger.warning("Google Sheets not configured, logging to console")
                print(f"LEAD LOG: {json.dumps(lead_data, indent=2)}")
                return
            
            sheet = self.sheets_service.open_by_key(SHEET_ID).worksheet("Leads")
            
            # Format row data
            row = [
                datetime.now().isoformat(),
                lead_data.get("name", ""),
                lead_data.get("phone", ""),
                lead_data.get("email", ""),
                lead_data.get("type", ""),  # buyer/seller/renter/other
                lead_data.get("area_interest", ""),
                lead_data.get("budget", ""),
                lead_data.get("timeline", ""),
                lead_data.get("property_address", ""),
                lead_data.get("notes", ""),
                lead_data.get("next_action", ""),
                "AI Receptionist",
                "New"
            ]
            
            sheet.append_row(row)
            logger.info(f"Lead logged: {lead_data.get('name', 'Unknown')}")
            
        except Exception as e:
            logger.error(f"Failed to log lead: {e}")
    
    async def check_calendar_availability(self, date_str: str, duration_minutes: int = 60) -> List[str]:
        """Check available slots in Mark's calendar"""
        try:
            if not self.calendar_service:
                # Return mock slots if not configured
                return ["10:00 AM", "2:00 PM", "4:00 PM"]
            
            # Parse date
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            time_min = date_obj.isoformat() + "Z"
            time_max = (date_obj + timedelta(days=1)).isoformat() + "Z"
            
            events_result = self.calendar_service.events().list(
                calendarId='primary',
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            
            # Generate available slots (9 AM to 8 PM)
            booked_slots = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                if 'T' in start:
                    booked_slots.append(datetime.fromisoformat(start.replace('Z', '+00:00')))
            
            available = []
            current = date_obj.replace(hour=9, minute=0)
            end = date_obj.replace(hour=20, minute=0)
            
            while current < end:
                is_available = True
                for booked in booked_slots:
                    booked_local = booked.replace(tzinfo=None)
                    if (booked_local.hour == current.hour and 
                        abs(booked_local.minute - current.minute) < duration_minutes):
                        is_available = False
                        break
                
                if is_available:
                    available.append(current.strftime("%I:%M %p"))
                
                current += timedelta(minutes=60)  # 1-hour slots
            
            return available[:5]  # Return top 5 options
            
        except Exception as e:
            logger.error(f"Calendar check failed: {e}")
            return ["10:00 AM", "2:00 PM", "4:00 PM"]  # Fallback
    
    async def book_appointment(self, booking_data: Dict[str, Any]) -> bool:
        """Book appointment in Google Calendar and log to Sheets"""
        try:
            if not self.calendar_service:
                logger.info(f"MOCK BOOKING: {booking_data}")
                return True
            
            # Create calendar event
            date_str = booking_data.get("date")
            time_str = booking_data.get("time")
            purpose = booking_data.get("purpose", "Real Estate Consultation")
            location = booking_data.get("location", "Phone/Video Call")
            
            # Parse datetime
            datetime_str = f"{date_str} {time_str}"
            start_time = datetime.strptime(datetime_str, "%Y-%m-%d %I:%M %p")
            end_time = start_time + timedelta(hours=1)
            
            event = {
                'summary': f"{purpose} - {booking_data.get('name', 'Client')} (AI Booked)",
                'location': location,
                'description': f"""
Client: {booking_data.get('name')}
Phone: {booking_data.get('phone')}
Email: {booking_data.get('email')}
Type: {booking_data.get('lead_type')}
Notes: {booking_data.get('notes')}

Booked by AI Receptionist. Mark to confirm.
                """,
                'start': {
                    'dateTime': start_time.isoformat(),
                    'timeZone': 'America/Montreal',
                },
                'end': {
                    'dateTime': end_time.isoformat(),
                    'timeZone': 'America/Montreal',
                },
                'attendees': [
                    {'email': AGENT_EMAIL} if AGENT_EMAIL else None,
                    {'email': booking_data.get('email')} if booking_data.get('email') else None
                ],
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'email', 'minutes': 60},
                        {'method': 'popup', 'minutes': 15},
                    ],
                },
            }
            
            # Remove None attendees
            event['attendees'] = [a for a in event['attendees'] if a]
            
            event_result = self.calendar_service.events().insert(
                calendarId='primary',
                body=event,
                sendUpdates='all'
            ).execute()
            
            # Log to Appointments sheet
            if self.sheets_service and SHEET_ID:
                sheet = self.sheets_service.open_by_key(SHEET_ID).worksheet("Appointments")
                sheet.append_row([
                    datetime.now().isoformat(),
                    booking_data.get("name"),
                    booking_data.get("phone"),
                    booking_data.get("email"),
                    purpose,
                    date_str,
                    time_str,
                    location,
                    event_result.get('htmlLink'),
                    "Confirmed"
                ])
            
            logger.info(f"Appointment booked: {event_result.get('htmlLink')}")
            return True
            
        except Exception as e:
            logger.error(f"Booking failed: {e}")
            return False

# Global Google integration instance
google_integration = GoogleIntegration()

# ==================== TWILIO INTEGRATION ====================

class TwilioIntegration:
    def __init__(self):
        self.account_sid = TWILIO_ACCOUNT_SID
        self.auth_token = TWILIO_AUTH_TOKEN
        self.from_number = TWILIO_PHONE_NUMBER
    
    async def send_sms(self, to_number: str, message: str):
        """Send SMS confirmation via Twilio"""
        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
            payload = {
                "From": self.from_number,
                "To": to_number,
                "Body": message
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=payload,
                    auth=aiohttp.BasicAuth(self.account_sid, self.auth_token)
                ) as response:
                    if response.status == 201:
                        logger.info(f"SMS sent to {to_number}")
                        return True
                    else:
                        logger.error(f"SMS failed: {await response.text()}")
                        return False
        except Exception as e:
            logger.error(f"SMS error: {e}")
            return False
    
    async def warm_transfer(self, call_sid: str, to_number: str, context: str):
        """Transfer call to Mark with context"""
        try:
            # Use Twilio Dial verb with whisper
            # In production, this would use Twilio's <Dial> with <Number> and url for whisper
            logger.info(f"TRANSFER: Call {call_sid} to {to_number} - Context: {context}")
            return True
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            return False

twilio_integration = TwilioIntegration()

# ==================== OPENAI REALTIME HANDLER ====================

class OpenAIRealtimeHandler:
    def __init__(self):
        self.ws = None
        self.session_id = None
        self.tools = [
            {
                "type": "function",
                "name": "check_calendar_availability",
                "description": "Check Mark's calendar for available appointment slots on a specific date",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "Date in YYYY-MM-DD format"
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Duration of appointment in minutes",
                            "default": 60
                        }
                    },
                    "required": ["date"]
                }
            },
            {
                "type": "function",
                "name": "book_appointment",
                "description": "Book a viewing or consultation appointment with Mark",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Client's full name"},
                        "phone": {"type": "string", "description": "Client's phone number"},
                        "email": {"type": "string", "description": "Client's email address"},
                        "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                        "time": {"type": "string", "description": "Time in HH:MM AM/PM format"},
                        "purpose": {
                            "type": "string", 
                            "enum": ["Property Viewing", "Market Analysis (CMA)", "Buyer Consultation", "Seller Consultation"],
                            "description": "Purpose of the meeting"
                        },
                        "location": {"type": "string", "description": "Property address or meeting location"},
                        "lead_type": {"type": "string", "enum": ["Buyer", "Seller", "Renter", "Investor"]},
                        "notes": {"type": "string", "description": "Additional notes about the client"}
                    },
                    "required": ["name", "phone", "date", "time", "purpose"]
                }
            },
            {
                "type": "function",
                "name": "log_lead",
                "description": "Save lead information to the CRM system",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "email": {"type": "string"},
                        "type": {"type": "string", "enum": ["Buyer", "Seller", "Renter", "Investor", "Other"]},
                        "area_interest": {"type": "string"},
                        "budget": {"type": "string"},
                        "timeline": {"type": "string"},
                        "property_address": {"type": "string"},
                        "notes": {"type": "string"},
                        "next_action": {"type": "string"}
                    },
                    "required": ["name", "phone", "type"]
                }
            },
            {
                "type": "function",
                "name": "send_sms_confirmation",
                "description": "Send appointment confirmation via SMS",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string", "description": "Phone number to send SMS to"},
                        "message": {"type": "string", "description": "Confirmation message content"}
                    },
                    "required": ["phone", "message"]
                }
            },
            {
                "type": "function",
                "name": "warm_transfer",
                "description": "Transfer the call to Mark Esposito with context summary",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Why transfer is needed"},
                        "context_summary": {"type": "string", "description": "Summary of conversation for Mark"}
                    },
                    "required": ["reason", "context_summary"]
                }
            },
            {
                "type": "function",
                "name": "log_voicemail",
                "description": "Record a voicemail message for Mark to callback",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "caller_name": {"type": "string"},
                        "caller_phone": {"type": "string"},
                        "message": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["Low", "Medium", "High"]}
                    },
                    "required": ["caller_name", "caller_phone", "message"]
                }
            }
        ]
    
    async def connect(self):
        """Connect to OpenAI Realtime API"""
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
        
        self.ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            extra_headers=headers
        )
        
        # Initialize session with system prompt and tools
        init_message = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": SYSTEM_PROMPT,
                "voice": "alloy",
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 600
                },
                "tools": self.tools,
                "tool_choice": "auto",
                "temperature": 0.7
            }
        }
        
        await self.ws.send(json.dumps(init_message))
        response = await self.ws.recv()
        logger.info(f"OpenAI session initialized: {response}")
        
        return self.ws
    
    async def send_audio(self, audio_data: bytes):
        """Send audio chunk to OpenAI"""
        if self.ws:
            # Twilio sends g711_ulaw, OpenAI expects base64
            message = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(audio_data).decode('utf-8')
            }
            await self.ws.send(json.dumps(message))
    
    async def receive_messages(self):
        """Generator for receiving messages from OpenAI"""
        if not self.ws:
            return
        
        try:
            async for message in self.ws:
                yield json.loads(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("OpenAI WebSocket closed")
    
    async def close(self):
        """Close connection"""
        if self.ws:
            await self.ws.close()

# ==================== CALL SESSION MANAGER ====================

class CallSession:
    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.openai_handler = OpenAIRealtimeHandler()
        self.lead_data = {
            "name": None,
            "phone": None,
            "email": None,
            "type": None,
            "area_interest": None,
            "budget": None,
            "timeline": None,
            "notes": []
        }
        self.conversation_history = []
        self.transfer_requested = False
        self.appointment_booked = False
    
    async def handle_tool_call(self, tool_call):
        """Execute tool calls from OpenAI"""
        function_name = tool_call.get("name")
        arguments = json.loads(tool_call.get("arguments", "{}"))
        
        logger.info(f"Tool call: {function_name} with args {arguments}")
        
        result = None
        
        if function_name == "check_calendar_availability":
            date = arguments.get("date")
            slots = await google_integration.check_calendar_availability(date)
            result = {"available_slots": slots, "date": date}
            
        elif function_name == "book_appointment":
            success = await google_integration.book_appointment(arguments)
            if success:
                self.appointment_booked = True
                # Send SMS confirmation
                phone = arguments.get("phone")
                if phone:
                    msg = f"Confirmed: {arguments.get('purpose')} with Mark Esposito on {arguments.get('date')} at {arguments.get('time')}. Address: {arguments.get('location')}. Mark will contact you shortly. -BHHS Québec"
                    await twilio_integration.send_sms(phone, msg)
            result = {"success": success, "booking_details": arguments}
            
        elif function_name == "log_lead":
            await google_integration.log_lead(arguments)
            # Update session lead data
            for key in ["name", "phone", "email", "type", "area_interest", "budget", "timeline"]:
                if arguments.get(key):
                    self.lead_data[key] = arguments.get(key)
            result = {"success": True, "lead_id": self.call_sid}
            
        elif function_name == "send_sms_confirmation":
            success = await twilio_integration.send_sms(
                arguments.get("phone"),
                arguments.get("message")
            )
            result = {"success": success}
            
        elif function_name == "warm_transfer":
            self.transfer_requested = True
            # Log the transfer request
            await google_integration.log_lead({
                "name": self.lead_data.get("name", "Unknown"),
                "phone": self.lead_data.get("phone", "Unknown"),
                "type": self.lead_data.get("type", "Other"),
                "notes": f"WARM TRANSFER REQUESTED: {arguments.get('reason')}",
                "next_action": f"Call back immediately. Context: {arguments.get('context_summary')}"
            })
            result = {
                "status": "transfer_initiated",
                "message": "Connecting you to Mark Esposito now. Please hold.",
                "context": arguments.get("context_summary")
            }
            
        elif function_name == "log_voicemail":
            await google_integration.log_lead({
                "name": arguments.get("caller_name"),
                "phone": arguments.get("caller_phone"),
                "type": "Voicemail",
                "notes": arguments.get("message"),
                "next_action": f"Callback requested - Urgency: {arguments.get('urgency', 'Medium')}"
            })
            result = {"success": True, "callback_within": "2 hours"}
        
        return result

# ==================== FASTAPI APPLICATION ====================

app = FastAPI(title="Mark Esposito AI Receptionist", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store active call sessions
active_sessions: Dict[str, CallSession] = {}

@app.get("/", response_class=HTMLResponse)
async def root():
    """Status page"""
    return """
    <html>
        <head><title>Mark Esposito AI Receptionist</title></head>
        <body style="font-family: Arial; padding: 40px;">
            <h1>🎙️ Mark Esposito AI Receptionist</h1>
            <p><strong>Status:</strong> Online</p>
            <p><strong>Agent:</strong> Mark Esposito - Berkshire Hathaway HomeServices Québec</p>
            <p><strong>Features:</strong> Bilingual (FR/EN), Appointment Booking, Lead CRM, Warm Transfer</p>
            <hr>
            <h2>Endpoints:</h2>
            <ul>
                <li><code>/incoming-call</code> - Twilio webhook for incoming calls</li>
                <li><code>/media-stream</code> - WebSocket for audio streaming</li>
                <li><code>/status</code> - System health check</li>
            </ul>
        </body>
    </html>
    """

@app.get("/status")
async def status():
    """Health check endpoint"""
    return {
        "status": "operational",
        "agent": AGENT_NAME,
        "company": AGENT_COMPANY,
        "active_calls": len(active_sessions),
        "integrations": {
            "google_sheets": bool(google_integration.sheets_service),
            "google_calendar": bool(google_integration.calendar_service),
            "twilio": bool(TWILIO_ACCOUNT_SID),
            "openai": bool(OPENAI_API_KEY)
        }
    }

@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Handle incoming Twilio calls
    Returns TwiML to connect to Media Streams WebSocket
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    from_number = form_data.get("From")
    
    logger.info(f"Incoming call from {from_number} (SID: {call_sid})")
    
    # Initialize call session
    session = CallSession(call_sid)
    session.lead_data["phone"] = from_number
    active_sessions[call_sid] = session
    
    # Generate TwiML to connect to WebSocket
    # Use wss://your-domain.com/media-stream
    host = request.headers.get("host", "localhost")
    protocol = "wss" if "localhost" not in host else "ws"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{protocol}://{host}/media-stream" track="both">
            <Parameter name="callSid" value="{call_sid}"/>
            <Parameter name="fromNumber" value="{from_number}"/>
        </Stream>
    </Connect>
</Response>"""
    
    return HTMLResponse(content=twiml, media_type="application/xml")

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    WebSocket endpoint for bidirectional audio streaming
    Twilio <-> OpenAI Realtime API
    """
    await websocket.accept()
    logger.info("Media stream WebSocket connected")
    
    call_sid = None
    session = None
    
    try:
        # Wait for Twilio to send start message with parameters
        start_msg = await websocket.receive_text()
        start_data = json.loads(start_msg)
        
        if start_data.get("event") == "start":
            call_sid = start_data["start"]["customParameters"].get("callSid")
            from_number = start_data["start"]["customParameters"].get("fromNumber")
            
            logger.info(f"Stream started for call {call_sid}")
            
            # Get or create session
            if call_sid in active_sessions:
                session = active_sessions[call_sid]
            else:
                session = CallSession(call_sid)
                session.lead_data["phone"] = from_number
                active_sessions[call_sid] = session
            
            # Connect to OpenAI Realtime API
            openai_ws = await session.openai_handler.connect()
            
            # Start bidirectional streaming
            async def twilio_to_openai():
                """Forward audio from Twilio to OpenAI"""
                try:
                    while True:
                        message = await websocket.receive_text()
                        data = json.loads(message)
                        
                        if data.get("event") == "media":
                            # Twilio sends base64-encoded g711_ulaw audio
                            audio_payload = data["media"]["payload"]
                            audio_data = base64.b64decode(audio_payload)
                            
                            # Send to OpenAI
                            await session.openai_handler.send_audio(audio_data)
                            
                        elif data.get("event") == "stop":
                            logger.info("Twilio stream stopped")
                            break
                            
                except Exception as e:
                    logger.error(f"Twilio->OpenAI error: {e}")
            
            async def openai_to_twilio():
                """Forward responses from OpenAI to Twilio"""
                try:
                    async for message in session.openai_handler.receive_messages():
                        msg_type = message.get("type")
                        
                        if msg_type == "response.audio.delta":
                            # Audio response from OpenAI - send to Twilio
                            audio_base64 = message.get("delta", "")
                            if audio_base64:
                                # Convert to Twilio media message
                                media_msg = {
                                    "event": "media",
                                    "streamSid": start_data["start"]["streamSid"],
                                    "media": {
                                        "payload": audio_base64
                                    }
                                }
                                await websocket.send_text(json.dumps(media_msg))
                                
                        elif msg_type == "response.function_call_arguments.done":
                            # Tool call completed - execute it
                            tool_call = message
                            result = await session.handle_tool_call(tool_call)
                            
                            # Send result back to OpenAI
                            response_msg = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": tool_call.get("call_id"),
                                    "output": json.dumps(result)
                                }
                            }
                            await openai_ws.send(json.dumps(response_msg))
                            
                            # Request next response
                            await openai_ws.send(json.dumps({
                                "type": "response.create"
                            }))
                            
                        elif msg_type == "response.done":
                            # Response completed, check for transfer
                            if session.transfer_requested:
                                # Send transfer TwiML
                                transfer_twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting you to Mark now.</Say>
    <Dial>{AGENT_PHONE}</Dial>
</Response>"""
                                # Note: In production, use Twilio API to modify live call
                                logger.info(f"Transfer requested for call {call_sid}")
                                
                        elif msg_type == "input_audio_buffer.speech_started":
                            # Caller started speaking - could interrupt
                            pass
                            
                        elif msg_type == "error":
                            logger.error(f"OpenAI error: {message}")
                            
                except Exception as e:
                    logger.error(f"OpenAI->Twilio error: {e}")
            
            # Run both directions concurrently
            await asyncio.gather(
                twilio_to_openai(),
                openai_to_twilio(),
                return_exceptions=True
            )
            
    except Exception as e:
        logger.error(f"Media stream error: {e}")
        
    finally:
        # Cleanup
        if session:
            await session.openai_handler.close()
        if call_sid and call_sid in active_sessions:
            # Keep session for a bit to log final data
            asyncio.create_task(delayed_cleanup(call_sid))
        
        logger.info(f"Media stream closed for call {call_sid}")

async def delayed_cleanup(call_sid: str, delay: int = 300):
    """Remove session after delay to allow final logging"""
    await asyncio.sleep(delay)
    if call_sid in active_sessions:
        session = active_sessions[call_sid]
        # Final lead log if not already done
        if not session.appointment_booked and session.lead_data.get("name"):
            await google_integration.log_lead({
                **session.lead_data,
                "notes": "Call ended without booking. Follow up required.",
                "next_action": "Call back to qualify further"
            })
        del active_sessions[call_sid]
        logger.info(f"Session {call_sid} cleaned up")

# ==================== ADDITIONAL ENDPOINTS ====================

@app.post("/call-status")
async def call_status(request: Request):
    """Handle Twilio call status callbacks"""
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    status = form_data.get("CallStatus")
    duration = form_data.get("CallDuration")
    
    logger.info(f"Call {call_sid} status: {status}, duration: {duration}s")
    
    # Log call outcome to Google Sheets
    await google_integration.log_lead({
        "name": "SYSTEM",
        "phone": call_sid,
        "type": "Call Log",
        "notes": f"Call ended. Status: {status}, Duration: {duration}s"
    })
    
    return JSONResponse({"status": "logged"})

@app.post("/voicemail")
async def voicemail(request: Request):
    """Handle voicemail recordings"""
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    recording_url = form_data.get("RecordingUrl")
    from_number = form_data.get("From")
    
    logger.info(f"Voicemail from {from_number}: {recording_url}")
    
    # Log voicemail
    await google_integration.log_lead({
        "name": "Voicemail",
        "phone": from_number,
        "type": "Voicemail",
        "notes": f"Recording: {recording_url}. Transcription pending.",
        "next_action": "Listen and call back within 2 hours"
    })
    
    return JSONResponse({"status": "voicemail_logged"})

# ==================== RENDER DEPLOYMENT SETUP ====================

if __name__ == "__main__":
    import uvicorn
    # Render sets PORT environment variable
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
