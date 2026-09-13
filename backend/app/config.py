"""Settings, loaded from .env. Values are never logged."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)

# Same .env chain the CLI probe uses, so credentials live in one place.
ENV_CANDIDATES = [
    os.path.join(BACKEND, ".env"),
    os.path.join(ROOT, ".env"),
    os.path.join(ROOT, "experiments", "voice-feasibility", ".env"),
]

# Hard-coded on purpose. This is the only number the evaluator owns, and it is
# never dialled by anything except a deliberate, explicit production run.
EVALUATOR_NUMBER = "+918688664337"


def digits_of(number):
    """Just the digits, so +91-830 994 2858 and 918309942858 compare equal."""
    return "".join(ch for ch in (number or "") if ch.isdigit())


def same_number(a, b):
    """Last ten digits, which is what identifies an Indian mobile regardless of
    whether the country code or a leading zero was written."""
    da, dbn = digits_of(a), digits_of(b)
    return bool(da) and bool(dbn) and da[-10:] == dbn[-10:]


def is_evaluator(number):
    return same_number(number, EVALUATOR_NUMBER)


def load_env():
    """Populate os.environ from the first .env found. Returns its path."""
    for path in ENV_CANDIDATES:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip("'\""))
        return path
    return None


class Settings:
    """Read once at import. Restart the server to pick up .env changes."""

    def __init__(self):
        self.env_file = load_env()

        self.vapi_api_key = os.environ.get("VAPI_API_KEY", "")
        self.vapi_phone_number_id = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
        self.vapi_assistant_id = os.environ.get("VAPI_ASSISTANT_ID", "")

        # THE safety control. The trigger endpoint takes no phone number as
        # input - it can only ever dial this one. Defaults to TEST_NUMBER so a
        # misconfigured local run rings our own phone, never the evaluator's.
        self.allowed_destination = (
            os.environ.get("ALLOWED_DESTINATION")
            or os.environ.get("TEST_NUMBER", "")
        ).strip()

        # Optional shared secret. If set, webhook requests must carry it in
        # x-vapi-secret. Unset locally is fine; set it before deploying.
        self.webhook_secret = os.environ.get("VAPI_WEBHOOK_SECRET", "")

        self.db_path = os.environ.get(
            "DATABASE_PATH", os.path.join(BACKEND, "data", "elevatebox.db")
        )
        self.timezone = "Asia/Kolkata"

        # --- WhatsApp: UltraMsg (unofficial gateway) or Meta Cloud API.
        #
        # Defaults to whichever is actually configured, preferring UltraMsg,
        # because Meta and Twilio both dead-ended on KYC and template approval.
        # The switch is one env var so the official path can be taken back the
        # moment a verified sender exists - see app/ultramsg.py for the full
        # reasoning and the disclosure obligation that comes with it.
        self.um_instance = os.environ.get("ULTRAMSG_INSTANCE_ID", "").strip()
        self.um_token = os.environ.get("ULTRAMSG_TOKEN", "").strip()
        self.wa_provider = (os.environ.get("WHATSAPP_PROVIDER", "").strip().lower()
                            or ("ultramsg" if self.um_token else "meta"))

        # --- WhatsApp Cloud API
        self.wa_phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.wa_business_account_id = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
        self.wa_access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        self.wa_sender_number = os.environ.get("WHATSAPP_SENDER_NUMBER", "")
        self.wa_verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.wa_api_version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")

        # Media Meta must be able to fetch by URL (share links do not work).
        self.wa_resume_url = os.environ.get("WHATSAPP_RESUME_URL", "")
        self.wa_architecture_url = os.environ.get("WHATSAPP_ARCHITECTURE_IMAGE_URL", "")

        # Template names, overridable in case Meta approves them under different
        # names than the drafts in docs/whatsapp-templates.md.
        self.wa_tpl_midcall = os.environ.get("WHATSAPP_TEMPLATE_MIDCALL",
                                             "elevatebox_call_recap_now")
        self.wa_tpl_followup = os.environ.get("WHATSAPP_TEMPLATE_FOLLOWUP",
                                              "elevatebox_followup_context_2")
        self.wa_tpl_resume = os.environ.get("WHATSAPP_TEMPLATE_RESUME",
                                            "elevatebox_resume_delivery")
        self.wa_template_lang = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")

        # hello_world takes no variables. While our own template is in review we
        # point the mid-call action at it to prove the END-TO-END flow; the only
        # difference is the message body. Auto-detected, overridable.
        _explicit = os.environ.get("WHATSAPP_MIDCALL_PARAMS")
        self.wa_midcall_params = (_explicit != "0") if _explicit is not None             else (self.wa_tpl_midcall != "hello_world")
        self.wa_midcall_lang = os.environ.get(
            "WHATSAPP_MIDCALL_LANG",
            "en_US" if self.wa_tpl_midcall == "hello_world" else self.wa_template_lang)

        self.your_mobile = os.environ.get("YOUR_MOBILE_NUMBER", "")
        self.your_name = os.environ.get("YOUR_NAME", "").strip()
        # The assignment's "send to 8688664337" list names the live prototype and
        # the repository link as deliverables, so they are NOT optional extras -
        # a missing env var silently drops a required item from the message. They
        # default to the real published URLs rather than to "" for the same reason
        # EVALUATOR_NUMBER is hard-coded: these are public facts about the
        # submission, not credentials, and the deliverable must not depend on a
        # dashboard setting anyone can forget. An env var still overrides.
        self.demo_url = (os.environ.get("DEMO_URL", "").strip()
                         or "https://elevatebox-assignment-t9cg.onrender.com/")
        self.repo_url = (os.environ.get("REPO_URL", "").strip()
                         or "https://github.com/Thumulavamshi/ElevateBox_Assignment")
        self.note_url = (os.environ.get("NOTE_URL", "").strip()
                         or "https://github.com/Thumulavamshi/ElevateBox_Assignment"
                            "/blob/main/NOTE.md")
        self.wa_resume_filename = os.environ.get(
            "WHATSAPP_RESUME_FILENAME", "resume.pdf")

        # Deliberate, explicit opt-in before anything can reach the evaluator.
        self.allow_evaluator = os.environ.get("ALLOW_EVALUATOR", "") == "1"

        # --- Multi-app layer. Each app is optional: with no credentials it is
        # never dispatched to, and /health says which ones are live.
        self.google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        self.google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        self.google_refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
        # "primary" is the rep's own calendar. A dedicated one keeps test runs
        # out of their day and makes harness cleanup safe.
        self.google_calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip() or "primary"

        self.hubspot_token = os.environ.get("HUBSPOT_TOKEN", "").strip()
        self.hubspot_portal_id = os.environ.get("HUBSPOT_PORTAL_ID", "").strip()
        # Stage ids belong to the portal. The stock pipeline uses readable ids; a
        # customised one (ours) uses numbers, so these have to come from env.
        self.hubspot_pipeline_id = os.environ.get("HUBSPOT_PIPELINE_ID", "").strip() or "default"
        self.hubspot_stage_hot = (os.environ.get("HUBSPOT_STAGE_HOT", "").strip()
                                  or "qualifiedtobuy")
        self.hubspot_stage_warm = (os.environ.get("HUBSPOT_STAGE_WARM", "").strip()
                                   or "appointmentscheduled")

        self.slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        self.slack_channel_id = os.environ.get("SLACK_CHANNEL_ID", "").strip()

        # Where links in Slack, HubSpot and Calendar point back to. Falls back to
        # SERVER_URL, which agent.py already needs for the webhook.
        self.public_base_url = (os.environ.get("PUBLIC_BASE_URL", "").strip()
                                or os.environ.get("SERVER_URL", "").strip()).rstrip("/")
        self.vapi_public_key = os.environ.get("VAPI_PUBLIC_KEY", "").strip()

    def missing(self):
        """Which settings block placing a call. Read endpoints work without them."""
        required = {
            "VAPI_API_KEY": self.vapi_api_key,
            "VAPI_PHONE_NUMBER_ID": self.vapi_phone_number_id,
            "VAPI_ASSISTANT_ID": self.vapi_assistant_id,
            "ALLOWED_DESTINATION": self.allowed_destination,
        }
        return [k for k, v in required.items() if not v]

    def permits(self, number):
        """Is this number one we are allowed to contact? Returns (ok, why_not).

        The single place that answers the question, so the dialler, the WhatsApp
        sender and the callback worker cannot drift apart on it.

        They HAD drifted, and the failure mode was the worst kind: the dialler
        had no evaluator check at all while the WhatsApp sender did, so pointing
        ALLOWED_DESTINATION at the evaluator without also setting ALLOW_EVALUATOR
        produced a call that connected and held a perfect conversation while
        every single message - mid-call, callback confirmation, follow-up and
        resume - was silently refused. That is most of the scorecard lost on a
        call that looks flawless from the phone.
        """
        if not digits_of(number):
            return False, "no destination number"
        if is_evaluator(number) and not self.allow_evaluator:
            return False, ("refusing to contact the evaluator: set ALLOW_EVALUATOR=1 "
                           "deliberately for the real run")
        if not same_number(number, self.allowed_destination):
            return False, (f"{digits_of(number)[-10:]} is not the allowed "
                           f"destination {digits_of(self.allowed_destination)[-10:]}")
        return True, ""

    def evaluator_ready(self):
        """Whether a real evaluator run is armed CONSISTENTLY.

        Reported on /health because the dangerous state is not 'blocked', it is
        'half-armed': allowed to dial but not to message. Blocked is obvious in
        five seconds; half-armed is only discovered after the one call that
        mattered has already ended.
        """
        if not is_evaluator(self.allowed_destination):
            return False, "evaluator is not the ALLOWED_DESTINATION"
        if not self.allow_evaluator:
            return False, ("evaluator is dialable but ALLOW_EVALUATOR is not 1 - "
                           "the call would connect and every WhatsApp would be refused")
        return True, "armed: the evaluator can be called and messaged"


settings = Settings()
