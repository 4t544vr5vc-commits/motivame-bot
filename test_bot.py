import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

sys.path.insert(0, str(Path(__file__).parent))

# Mock environment variables for testing
os.environ.setdefault("MOTIVAME_TELEGRAM_TOKEN", "test-token-123")
os.environ.setdefault("MOTIVAME_OPENAI_KEY", "test-key-456")


class TestUserPersistence:
    def setup_method(self):
        import bot
        self.bot = bot
        self.test_file = Path("/tmp/test_users.json")
        self.bot.USERS_FILE = self.test_file
        if self.test_file.exists():
            self.test_file.unlink()

    def teardown_method(self):
        if self.test_file.exists():
            self.test_file.unlink()

    def test_load_users_empty(self):
        result = self.bot.load_users()
        assert result == {}

    def test_save_and_load_users(self):
        users = {"123": {"nome": "Marco", "obiettivo": "correre"}}
        self.bot.save_users(users)
        loaded = self.bot.load_users()
        assert loaded == users

    def test_set_and_get_user(self):
        self.bot.set_user("456", {"nome": "Luca", "obiettivo": "perdere peso"})
        user = self.bot.get_user("456")
        assert user["nome"] == "Luca"
        assert user["obiettivo"] == "perdere peso"

    def test_get_nonexistent_user(self):
        result = self.bot.get_user("999")
        assert result == {}

    def test_multiple_users(self):
        self.bot.set_user("1", {"nome": "Anna"})
        self.bot.set_user("2", {"nome": "Paolo"})
        assert self.bot.get_user("1")["nome"] == "Anna"
        assert self.bot.get_user("2")["nome"] == "Paolo"


class TestAskGpt:
    def test_ask_gpt_success(self):
        import bot
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Forza campione! 💪"

        with patch.object(bot.openai_client.chat.completions, "create", return_value=mock_response):
            result = bot.ask_gpt("Motivami!")
            assert "Forza" in result

    def test_ask_gpt_error_handling(self):
        import bot
        with patch.object(bot.openai_client.chat.completions, "create", side_effect=Exception("API Error")):
            result = bot.ask_gpt("test")
            assert "⚠️" in result
            assert "cramp" in result


class TestBotImport:
    def test_module_imports(self):
        import bot
        assert hasattr(bot, "main")
        assert hasattr(bot, "start")
        assert hasattr(bot, "allenamento")
        assert hasattr(bot, "alimentazione")
        assert hasattr(bot, "motivami")
        assert hasattr(bot, "progressi")
        assert hasattr(bot, "risposta_libera")

    def test_system_prompt_is_italian(self):
        import bot
        assert "italiano" in bot.SYSTEM_PROMPT.lower()

    def test_conversation_states(self):
        import bot
        assert bot.NOME == 0
        assert bot.OBIETTIVO == 1


