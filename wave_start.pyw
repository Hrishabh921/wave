import os
import sys
import logging
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow

os.chdir(os.path.dirname(os.path.abspath(__file__)))

def main():
    
    logging.info("Initializing Wave AI GUI in Background...")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    window = MainWindow()
    window.showMinimized() 
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()