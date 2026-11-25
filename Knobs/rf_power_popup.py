"""RF loss notification for runs that resume automatically."""
from PyQt6.QtWidgets import QMessageBox


def show_power_pause(parent, reason):
    box = QMessageBox(QMessageBox.Icon.Warning, "RF Power Auto Pause", reason,
                      QMessageBox.StandardButton.Close, parent)
    box.setInformativeText("The run resumes automatically after RF power recovers for 3 seconds.")
    box.setModal(False)
    box.show()
    return box
