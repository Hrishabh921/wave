"""
CSS/Style definitions for Wave AI Assistant GUI.
"""

JARVIS_THEME = """
QMainWindow {
    background-color: #050510; /* Deep Sci-Fi Black */
}

/* Sidebar Styling */
#Sidebar {
    background-color: #0a0a1a;
    border-right: 2px solid #00f0ff;
}

QPushButton {
    background-color: transparent;
    color: #00f0ff; /* Neon Cyan */
    font-size: 15px;
    font-weight: bold;
    text-align: left;
    padding: 12px 20px;
    border: 1px solid transparent;
    border-radius: 8px;
    margin: 4px 8px;
}

QPushButton:hover {
    background-color: rgba(0, 240, 255, 0.1);
    border: 1px solid #00f0ff;
}

QPushButton:checked {
    background-color: rgba(0, 240, 255, 0.2);
    color: #ffffff;
    border: 1px solid #00f0ff;
}

/* Chat Interface Styling */
QTextEdit {
    background-color: #020205;
    color: #e0f8ff;
    font-size: 15px;
    font-family: "Consolas", monospace; /* Hacker style font */
    border: 1px solid #004466;
    border-radius: 10px;
    padding: 15px;
}

QLineEdit {
    background-color: #0a0a1a;
    color: #00f0ff;
    font-size: 15px;
    border: 1px solid #00f0ff;
    border-radius: 20px;
    padding: 10px 15px;
}

QLineEdit:focus {
    border: 2px solid #00ffff;
    background-color: #050510;
}

/* Action Buttons */
#SendButton {
    background-color: transparent;
    color: #00f0ff;
    border: 1px solid #00f0ff;
    border-radius: 20px;
    font-weight: bold;
    padding: 10px 20px;
    text-align: center;
}

#SendButton:hover {
    background-color: #00f0ff;
    color: #000000;
}

/* Dashboard Cards */
.QFrame {
    background-color: #0a0a1a;
    border-radius: 10px;
    border: 1px solid #00f0ff;
}

QLabel {
    color: #00f0ff;
    font-family: "Consolas", monospace;
}
"""