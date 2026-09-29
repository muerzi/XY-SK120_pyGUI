import traceback, sys
#import subprocess
import glob
import serial
import time
#import os
import csv
import datetime
from pathlib import Path
from serial.tools import list_ports

from dps_modbus import Serial_modbus
from dps_modbus import Dps5005
from dps_modbus import Import_limits

from PyQt5.QtCore import pyqtSlot, pyqtSignal, QRunnable, QThreadPool, QTimer, QThread, QCoreApplication, QObject, QMutex, Qt
from PyQt5.QtWidgets import QApplication, QDialog, QFileDialog, QGraphicsView, QMainWindow, QMessageBox, QSlider, QAction
from PyQt5.QtGui import QIcon, QFont, QPixmap
from PyQt5.uic import loadUi

QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)

import pyqtgraph as pg
import numpy as np

dps = 0
dps_mode = 0 # 0 PSU default, 1 nicad, 2 li-ion, 3 CSV, 


def app_path(*parts):
	base_path = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
	return str(base_path.joinpath(*parts))


class ConnectionDialog(QDialog):
	def __init__(self, parent, connection_settings, connected, status_text):
		super(ConnectionDialog, self).__init__(parent)
		loadUi(app_path('connection_dialog.ui'), self)
		self.parent_gui = parent
		self.connection_settings = dict(connection_settings)
		self.action = None
		self.fixed_port = parent.limits.port_set
		self.setModal(True)
		self.comboBox_baudrate.addItems(["115200", "9600", "2400", "4800", "19200"])
		self.label_status.setText(status_text)
		self.lineEdit_slave.setText(str(self.connection_settings.get('slave_addr', '1')))
		self.comboBox_baudrate.setCurrentText(str(self.connection_settings.get('baudrate', '115200')))
		self.button_rescan.clicked.connect(self.populate_ports)
		self.button_connect.clicked.connect(self.accept_connect)
		self.button_disconnect.clicked.connect(self.accept_disconnect)
		self.button_disconnect.setVisible(connected)
		self.button_close.clicked.connect(self.reject)

		self.populate_ports()

		if self.fixed_port:
			self.comboBox_port.setEnabled(False)
			self.button_rescan.setEnabled(False)
			self.label_status.setText('Fixed port from ini: %s' % self.fixed_port)

	def populate_ports(self):
		current_port = str(self.connection_settings.get('port', ''))
		self.comboBox_port.clear()

		if self.fixed_port:
			self.comboBox_port.addItem(self.fixed_port, self.fixed_port)
			self.comboBox_port.setCurrentIndex(0)
			return

		self.comboBox_port.addItem('Auto detect', '')
		for port in self.parent_gui.scan_serial_ports():
			self.comboBox_port.addItem(port, port)

		index = self.comboBox_port.findData(current_port)
		if index >= 0:
			self.comboBox_port.setCurrentIndex(index)

	def accept_connect(self):
		try:
			slave_addr = abs(int(self.lineEdit_slave.text()))
			baudrate = abs(int(self.comboBox_baudrate.currentText()))
		except ValueError:
			QMessageBox.warning(self, 'Invalid settings', 'Slave address and baud rate must be numbers.')
			return

		self.connection_settings = {
			'port': self.fixed_port or self.comboBox_port.currentData(),
			'baudrate': str(baudrate),
			'slave_addr': str(slave_addr),
		}
		self.action = 'connect'
		self.accept()

	def accept_disconnect(self):
		self.action = 'disconnect'
		self.accept()


class WorkerSignals(QObject):
	finished = pyqtSignal()
	error = pyqtSignal(tuple)
	result = pyqtSignal(object)
	progress = pyqtSignal(int)

class Worker(QRunnable):
	def __init__(self, fn, *args, **kwargs):
		super(Worker, self).__init__()
		# Store constructor arguments (re-used for processing)
		self.fn = fn
		self.args = args
		self.kwargs = kwargs
		self.signals = WorkerSignals()

		# Add the callback to our kwargs
		kwargs['progress_callback'] = self.signals.progress

	@pyqtSlot()
	def run(self):
		'''
		Initialise the runner function with passed args, kwargs.
		'''
		try:
			result = self.fn(*self.args, **self.kwargs)
		except:
			traceback.print_exc()
			exctype, value = sys.exc_info()[:2]
			self.signals.error.emit((exctype, value, traceback.format_exc()))
		else:
			self.signals.result.emit(result)  # Return the result of the processing
		finally:
			self.signals.finished.emit()  # Done


class dps_GUI(QMainWindow):
	def __init__(self):
		self.limits = Import_limits(app_path("config.ini"))
			
		pg.setConfigOption('background', self.limits.background_colour)
			
		super(dps_GUI,self).__init__()
		loadUi(app_path('dps_GUI.ui'), self)
		if not hasattr(self, 'pushButton_connect') and hasattr(self, 'pushButton_connect_2'):
			self.pushButton_connect = self.pushButton_connect_2
		
		self.setWindowTitle('XY6015L_pyGUI')
		
		self.mutex = QMutex()
		
	#--- fix font style & size, mainly for HighDpiScaling
		f = QFont("Liberation Sans", 10)
		self.setFont(f)
	
	#--- PlotWidget
		self.pg_plot_setup()
		
	#--- threading
		self.threadpool = QThreadPool()
	#   print("Multithreading with maximum %d threads" % self.threadpool.maxThreadCount())
		
	#--- globals
		self.serialconnected = False
		self.connection_status_text = 'Disconnected'
		self.connection_settings = {'port': '', 'baudrate': '115200', 'slave_addr': '1'}
		self.connection_in_progress = False
		self.slider_in_use = False
		self.CSV_file = ''
		self.CSV_list = []
		self.graph_X = []
		self.graph_Y1 = []
		self.graph_Y2 = []
		self.graph_Y1_set = np.empty(shape=[0])
		self.graph_Y2_set = np.empty(shape=[0])
		self.time_old = ""
		self.capacity_time_old = ""
		self.capacity = 0.0
		
	#--- connect signals + keyboard shortcuts + status tips
		self.pushButton_save_plot.clicked.connect(self.pushButton_save_plot_clicked)
		self.pushButton_save_plot.setShortcut(Qt.CTRL | Qt.Key_S)					# 'Save Plot' - save file/plot *.csv
		self.pushButton_save_plot.setStatusTip('Save Plot - CTRL+S')
		
		self.pushButton_clear_plot.clicked.connect(self.pushButton_clear_plot_clicked)
		self.pushButton_clear_plot.setShortcut(Qt.CTRL | Qt.Key_L)					# 'Clear' - clear/new plot
		self.pushButton_clear_plot.setStatusTip('Clear Plot - CTRL+L')
		
		self.radioButton_lock.clicked.connect(self.radioButton_lock_clicked)
		self.radioButton_lock.setShortcut(Qt.CTRL | Qt.ALT | Qt.Key_L)				# 'Lock' - toggle status
		self.radioButton_lock.setStatusTip('Toggle Lock - CTRL+ALT+L')
			
		self.pushButton_onoff.clicked.connect(self.pushButton_onoff_clicked)		# On / Off
		
		self.pushButton_set.clicked.connect(self.pushButton_set_clicked)			# 'Set' - PSU
		self.pushButton_set_2.clicked.connect(self.pushButton_set_2_clicked)		# 'Set' - NiMH/NiCad
		self.pushButton_set_3.clicked.connect(self.pushButton_set_3_clicked)		# 'Set' - Li-Ion/Lipo
		
		self.pushButton_connect.clicked.connect(self.pushButton_connect_clicked)	# 'Connect'
		
		self.pushButton_CSV.clicked.connect(self.pushButton_CSV_clicked)			# 'CSV run'
		self.pushButton_CSV_clear.clicked.connect(self.pushButton_CSV_clear_clicked)# 'CSV clear'
		self.pushButton_CSV_view.clicked.connect(self.pushButton_CSV_view_clicked)	# 'CSV view'
		
		#self.horizontalSlider_brightness.valueChanged.connect(self.horizontalSlider_brightness_valueChanged)
		
		self.actionOpen.triggered.connect(self.file_open)
		self.actionOpen.setShortcut(Qt.CTRL | Qt.Key_O)								# File -> Open - open file *.csv
		self.actionOpen.setStatusTip('File Open - CTRL+O')
			
		self.actionQuit.triggered.connect(self.close)
		self.actionQuit.setShortcut(Qt.CTRL | Qt.Key_Q)								# File -> Quit - quit application
		self.actionQuit.setStatusTip('Quit application - CTRL+Q')
		self.setup_connection_menu()
		
	#--- do once on startup
		self.combobox_populate()
		self.setup_connection_controls()

	#--- setup & run background task
		self.timer2 = QTimer()
		self.timer2.setInterval(10)
		self.timer2.timeout.connect(self.action_CSV)
		
		self.timer = QTimer()
		self.timer.setInterval(self.limits.update_interval)
		self.timer.timeout.connect(self.loop_function)
		
	#--- V I knobs 
		self.dial_volt.valueChanged.connect(self.dial_volt_value_changed)	# 'dial volt'
		self.dial_volt.setStatusTip('Turn with mouse, arrows or PgUp')
		
		self.dial_curr.valueChanged.connect(self.dial_curr_value_changed)	# 'dial curr'
		self.dial_curr.setStatusTip('Turn with mouse, arrows or PgUp')
		
	#--- knobs maximum value
		self.dial_volt.setMaximum(int(self.limits.voltage_set_max * 10 ** self.limits.decimals_vset))
		self.dial_curr.setMaximum(int(self.limits.current_set_max * 10 ** self.limits.decimals_iset))
		
	#--- icons
		self.pix_on=QPixmap(app_path("icon", "led_on.png"))
		self.pix_off=QPixmap(app_path("icon", "led_off.png"))

	def setup_connection_controls(self):
		for widget_name in ['label', 'lineEdit_slave_addr', 'label_26', 'COMPortsComboBox', 'RefreshToolButton', 'label_28', 'comboBox_datarate']:
			if hasattr(self, widget_name):
				getattr(self, widget_name).setVisible(False)

		if hasattr(self, 'pushButton_connect'):
			self.pushButton_connect.hide()
			self.pushButton_connect.setCheckable(False)
			self.pushButton_connect.setMinimumWidth(100)
			self.pushButton_connect.setMaximumWidth(100)
		self.update_connection_button()

	def setup_connection_menu(self):
		self.menuConnection = self.menubar.addMenu('&Connection')
		self.actionConnection = QAction('Connection...', self)
		self.actionConnection.triggered.connect(self.pushButton_connect_clicked)
		self.actionConnection.setShortcut(Qt.CTRL | Qt.SHIFT | Qt.Key_C)
		self.menuConnection.addAction(self.actionConnection)

	def update_connection_button(self):
		if self.connection_in_progress:
			button_text = 'Connecting...'
		elif self.serialconnected:
			button_text = 'Connected...'
		else:
			button_text = 'Connection...'

		port_label = self.connection_settings.get('port') or 'Auto detect'
		tooltip = 'Status: %s\nPort: %s\nBaud: %s\nSlave: %s' % (
			self.connection_status_text,
			port_label,
			self.connection_settings.get('baudrate', '115200'),
			self.connection_settings.get('slave_addr', '1'),
		)
		if hasattr(self, 'pushButton_connect'):
			self.pushButton_connect.setText(button_text)
			self.pushButton_connect.setToolTip(tooltip)
			self.pushButton_connect.setStatusTip(tooltip)
			self.pushButton_connect.setEnabled(not self.connection_in_progress)
		if hasattr(self, 'actionConnection'):
			self.actionConnection.setText(button_text)
			self.actionConnection.setToolTip(tooltip)
			self.actionConnection.setStatusTip(tooltip)
			self.actionConnection.setEnabled(not self.connection_in_progress)
	
	def closeEvent(self, event):    
		self.shutdown() # switch OFF output when application closes to prevent unmonitored charging
		
	def shutdown(self):
		if self.pushButton_onoff.isChecked() == True:   
			self.label_onoff.setText('Output      :   OFF') # off
			self.pushButton_onoff.setChecked(False)
			self.pushButton_onoff_clicked()
			print("def shutdown")
			
	def pg_plot_setup(self): # right axis not connected to automatic scaling on the left ('A' icon on bottom LHD)
		self.p1 = self.graphicsView.plotItem
		self.p1.setClipToView(True)     

	# x axis    
		self.p1.setLabel('bottom', 'Time', units='s', color=self.limits.x_colour, **{'font-size':'10pt'})
		self.p1.getAxis('bottom').setPen(pg.mkPen(color=self.limits.x_colour, width=self.limits.x_pen_weight))

	# Y1 axis   
		self.p1.setLabel('left', 'Voltage', units='V', color=self.limits.y1_colour, **{'font-size':'10pt'})
		self.pen_Y1 = pg.mkPen(color=self.limits.y1_colour, width=self.limits.y1_pen_weight)
		self.p1.getAxis('left').setPen(self.pen_Y1)
		self.pen_Y1_set = pg.mkPen(color=self.limits.y1_set_colour, width=self.limits.y1_pen_weight*0.6)
	
	# setup viewbox for right hand axis
		self.p2 = pg.ViewBox()
		self.p1.showAxis('right')
		self.p1.scene().addItem(self.p2)
		self.p1.getAxis('right').linkToView(self.p2)
		self.p2.setXLink(self.p1)

	# Y2 axis
		self.p1.setLabel('right', 'Current', units="A", color=self.limits.y2_colour, **{'font-size':'10pt'})
		self.pen_Y2 = pg.mkPen(color=self.limits.y2_colour, width=self.limits.y2_pen_weight)
		self.p1.getAxis('right').setPen(self.pen_Y2)
		self.pen_Y2_set = pg.mkPen(color=self.limits.y2_set_colour, width=self.limits.y2_pen_weight*0.6)
		
	# scales ViewBox to scene
		self.p1.vb.sigResized.connect(self.updateViews) 	
		
		
	def updateViews(self):
		self.p2.setGeometry(self.p1.vb.sceneBoundingRect())
		self.p2.linkedViewChanged(self.p1.vb, self.p2.XAxis)

#--- update graph
	def update_graph_plot(self, chart_type = 'step'):
		start = time.time()
		X = np.asarray(self.graph_X, dtype=np.float32)
		Y1 = np.asarray(self.graph_Y1, dtype=np.float32)
		Y2 = np.asarray(self.graph_Y2, dtype=np.float32)
		Y1_set = self.graph_Y1_set
		Y2_set = self.graph_Y2_set

		if chart_type == 'step':		
			b = []
			for a in X:
				if len(b) == 0:
					b.append(a)
				else:
					b.append(a - 0.000001)	
					b.append(a)
			c = len(b)
			X = np.asarray(b, dtype=np.float32)

			def adapt_Y(Y):
				b = []
				for a in Y:
					b.append(a)
					if len(b) != c:
						b.append(a)
				return np.asarray(b, dtype=np.float32)
			Y1 = adapt_Y(Y1)
			Y2 = adapt_Y(Y2)
			Y1_set = adapt_Y(Y1_set)
			Y2_set = adapt_Y(Y2_set)

		self.p1.clear()
		self.p2.clear()
		
		self.p1.plot(X,Y1,pen=self.pen_Y1, name="V")
		self.p2.addItem(pg.PlotCurveItem(X,Y2,pen=self.pen_Y2, name="I"))	

		self.p1.addItem(pg.PlotCurveItem(X, Y1_set, pen=self.pen_Y1_set, name="V set"))
		self.p2.addItem(pg.PlotCurveItem(X, Y2_set, pen=self.pen_Y2_set, name="I set"))

		app.processEvents()
		
		a = (time.time() - start) * 1000.0
		self.label_plot_rate.setText(("Plot Rate  : %8.3fms" % (a)))
		
#--- file handling
	def file_open(self):
		filename = QFileDialog.getOpenFileName(self, "Open File", '', 'CSV(*.csv)')
		if filename[0] != '':
			self.CSV_file = filename[0]
		if self.CSV_file != '':
			self.open_CSV(self.CSV_file)    
	
	def file_save(self):
		filename, _ = QFileDialog.getSaveFileName(self, "Save File", datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")+".csv", "All Files (*);; CSV Files (*.csv)")
		if filename != '':
			rows = zip(self.graph_X, self.graph_Y1, self.graph_Y2)
			
			if sys.platform.startswith('win'):
				with open(filename, 'w', newline='') as f:					# added newline to prevent additional carriage return in windows (\r\r\n)
					writer = csv.writer(f)
					row = ['time(s)','voltage(V)','current(A)']
					writer.writerow(row)
					for row in rows:
						writer.writerow(row)
			elif sys.platform.startswith('linux') or sys.platform.startswith('cygwin'):
				with open(filename, 'w') as f:					# added newline to prevent additional carriage return in windows (\r\r\n)
					writer = csv.writer(f)
					row = ['time(s)','voltage(V)','current(A)']
					writer.writerow(row)
					for row in rows:
						writer.writerow(row)
			elif sys.platform.startswith('darwin'):
				with open(filename, 'w') as f:					# added newline to prevent additional carriage return in windows (\r\r\n)
					writer = csv.writer(f)
					row = ['time(s)','voltage(V)','current(A)']
					writer.writerow(row)
					for row in rows:
						writer.writerow(row)
			else:
				raise EnvironmentError('Unsupported platform')
			
			print("Note: Saving of the set voltage and set current is not implemented yet.")
			
			
			
		#	with open(filename, 'w', newline='') as f:					# added newline to prevent additional carriage return in windows (\r\r\n)
		#		writer = csv.writer(f)
		#		row = ['time(s)','voltage(V)','current(A)']
		#		writer.writerow(row)
		#		for row in rows:
		#			writer.writerow(row)
		
#--- thread related code
	def progress_fn(self, n):
		print("%d%% done" % n)
		
	def print_output(self, s):
		#print(s)
		pass
		
	def thread_complete(self):
		#print("THREAD COMPLETE!")
		pass
	
#--- buttons
	def pushButton_save_plot_clicked(self):
		self.file_save()

	def pushButton_clear_plot_clicked(self): 
		self.graph_X = []
		self.graph_Y1 = []
		self.graph_Y2 = []
		self.graph_Y1_set = np.empty(shape=[0])
		self.graph_Y2_set = np.empty(shape=[0])
		self.time_old = time.time()
		self.p1.clear()
		self.p2.clear()
		self.capacity_time_old = time.time()
		self.capacity = 0.0
		
	def dial_volt_value_changed(self, val):
		self.lineEdit_vset.setText(str(val / 10 ** self.limits.decimals_vset))
		
	def dial_curr_value_changed(self, val):
		self.lineEdit_iset.setText(str(val / 10 ** self.limits.decimals_iset))
	
	def radioButton_lock_clicked(self):
		if self.radioButton_lock.isChecked():
			self.pass_2_thread(self.lock_on_change)
		else:
			self.pass_2_thread(self.lock_off_change)
		
	# pass_2_thread - radioButton_lock_clicked
	def lock_on_change(self, progress_callback):
		self.pass_2_dps('lock', 'w', str(1))
	def lock_off_change(self, progress_callback):
		self.pass_2_dps('lock', 'w', str(0))

	def pushButton_onoff_clicked(self):
		if self.pushButton_onoff.isChecked():
			self.pushButton_on_start_time = time.time()
			self.pass_2_thread(self.on_change)
		else:
			self.pushButton_on_start_time = 0
			self.pass_2_thread(self.off_change)
		
	# pass_2_thread - pushButton_onoff_clicked
	def on_change(self, progress_callback):
		self.pass_2_dps('onoff', 'w', str(1))
	def off_change(self, progress_callback):
		self.pass_2_dps('onoff', 'w', str(0))
		
	# PSU mode - import values
	def pushButton_set_clicked(self):                   
		if self.lineEdit_vset.text() != '' or self.lineEdit_iset.text() != '':
			try:
				value1 = abs(float(self.lineEdit_vset.text()))	# added abs() to prevent applying incorrect sign
			except ValueError:
				self.lineEdit_vset.setText("Number ?")
				return
			try:
				value2 = abs(float(self.lineEdit_iset.text()))
			except ValueError:
				self.lineEdit_iset.setText("Number ?")
				return
			global dps_mode
			dps_mode = 0
			self.pass_2_dps('write_voltage_current', 'w', [value1, value2])
	
	# Nicad mode - import values
	def pushButton_set_2_clicked(self):                 
		if self.lineEdit_vset_2.text() != '' or self.lineEdit_iset_2.text() != '' or self.lineEdit_term_2.text() != '':
			try:
				value1 = abs(float(self.lineEdit_vset_2.text()))	# added abs() to prevent applying incorrect sign
			except ValueError:
				self.lineEdit_vset_2.setText("Number ?")
				return
			try:
				value2 = abs(float(self.lineEdit_iset_2.text()))
			except ValueError:
				self.lineEdit_iset_2.setText("Number ?")
				return
			try:
				value3 = abs(float(self.lineEdit_term_2.text()))
			except ValueError:
				self.lineEdit_term_2.setText("Number ?")
				return
			global dps_mode
			dps_mode = 1
			self.v_terminate = value3
			#print(self.v_terminate)
			self.v_peak = 0
			self.pass_2_dps('write_voltage_current', 'w', [value1, value2])
	
	# Li-ion mode - import values
	def pushButton_set_3_clicked(self):                 
		if self.lineEdit_vset_3.text() != '' or self.lineEdit_iset_3.text() != '' or self.lineEdit_term_3.text() != '':
			try:
				value1 = abs(float(self.lineEdit_vset_3.text()))	# added abs() to prevent applying incorrect sign
			except ValueError:
				self.lineEdit_vset_3.setText("Number ?")
				return
			try:
				value2 = abs(float(self.lineEdit_iset_3.text()))
			except ValueError:
				self.lineEdit_iset_3.setText("Number ?")
				return
			try:
				value3 = abs(float(self.lineEdit_term_3.text()))	
			except ValueError:
				self.lineEdit_term_3.setText("Number ?")
				return
			global dps_mode
			dps_mode = 2
			self.i_terminate = value3
			self.pass_2_dps('write_voltage_current', 'w', [value1, value2])		
			
	def pushButton_connect_clicked(self):
		if self.connection_in_progress:
			return

		dialog = ConnectionDialog(self, self.connection_settings, self.serialconnected, self.connection_status_text)
		if dialog.exec_() == QDialog.Accepted:
			if dialog.action == 'disconnect':
				self.serial_disconnect("Disconnected")
			elif dialog.action == 'connect':
				self.connection_settings = dialog.connection_settings
				if self.serialconnected:
					self.serial_disconnect("Disconnected")
				self.start_serial_connect(self.connection_settings)
	
	def pushButton_CSV_clicked(self):
		if len(self.CSV_list) > 0:
			global dps_mode
			dps_mode = 3		# set to CSV mode
			self.timer2.start()	# begin 
	
	def pushButton_CSV_clear_clicked(self):
		self.stop_CSV()

	def pushButton_CSV_view_clicked(self):
		if len(self.CSV_list) > 0:
			if self.serialconnected == False:	
				self.graph_X = [row[0] for row in self.CSV_list]		# Xaxis  - time interval
				self.graph_Y1 = [row[1] for row in self.CSV_list]				# Y1axis - voltage
				self.graph_Y2 = [row[2] for row in self.CSV_list]				# Y2axis - current
				self.update_graph_plot()
			else:
				pass
		
#--- import CSV file        
	def open_CSV(self, filename):
		self.CSV_list = []
		with open(filename, 'r') as f:
			csvReader = csv.reader(f)#, delimiter=',')  # reads file
			next(csvReader, None)                       # skips header
			data_list = list(csvReader)
			for row in data_list:
				if len(row) > 2:
					self.CSV_list.append(row)
			#self.CSV_list = data_list
		self.labelCSV(len(self.CSV_list))   
				
	def labelCSV(self, value):          # display remaining steps
		self.label_CSV.setText("Steps remaining: %3d" % value)

#--- action the imported CSV using timer2       
	def action_CSV(self):
		if self.pushButton_onoff.isChecked() == True: 
			global dps_mode
			if dps_mode != 3:
				return  
			if len(self.CSV_list) > 0:
				data_list = self.CSV_list
				
				if len(self.CSV_list) > 1:			# calculate step time interval
					value0 = float(data_list[1][0]) - float(data_list[0][0])
				else:
					value0 = 0
				
				# set Voltage/Current levels
				value1 = float(data_list[0][1])
				value2 = float(data_list[0][2])
				self.pass_2_dps('write_voltage_current', 'w', [value1, value2])
				
				data_list.pop(0)
				self.timer2.stop()
				self.timer2.setInterval(int(value0)*1000)
				self.timer2.start()
				self.labelCSV(len(self.CSV_list)) 	# display No. of remaining steps
			else:
				self.stop_CSV()

	def stop_CSV(self):
		self.timer2.stop()
		self.CSV_list = []
		self.labelCSV(len(self.CSV_list)) 
		global dps_mode
		dps_mode = 0	# return to PSU mode
		
#--- slider 
	def horizontalSlider_brightness_valueChanged(self):
		self.pass_2_thread(self.slider_change)
	
	# pass_2_thread - horizontalSlider_brightness_valueChanged
	def slider_change(self, progress_callback):
		value = self.horizontalSlider_brightness.value()
		self.pass_2_dps('b_led', 'w', str(value))

#--- thread the needle  
	def pass_2_thread(self, func):
		# Pass the function to execute
		worker = Worker(func) # Any other args, kwargs are passed to the run function
		worker.signals.result.connect(self.print_output)
		worker.signals.finished.connect(self.thread_complete)
		worker.signals.progress.connect(self.progress_fn)
		self.threadpool.start(worker)

#--- loop is actioned from timer1, reading data & controlling charging  
	def loop_function(self):
		try:
			if self.serialconnected == False:
				self.serial_connect(self.connection_settings)
			self.read_all()
			self.operating_mode()
		except:
			self.serial_disconnect("Disconnected")
		
#--- operating mode 
	def operating_mode(self):
		global dps_mode
		value = dps_mode
		if value == 0:
			self.label_operating_mode.setText('PSU')
		elif value == 1:
			self.label_operating_mode.setText('NiMH')
			if float(self.vout_str) > float(self.v_peak):   # find peak voltage
				self.v_peak = float(self.vout_str)
			if self.pushButton_onoff.isChecked() and (time.time() - self.pushButton_on_start_time > 5): # adds 5sec delay, to prevent immediate switch OFF
				if float(self.vout_str) <= (self.v_peak - float(self.v_terminate)):     # switch off output
					self.pushButton_onoff.setChecked(False)
					self.pushButton_onoff_clicked()
		elif value == 2:
			self.label_operating_mode.setText('Li-Ion')
			if self.pushButton_onoff.isChecked() and (time.time() - self.pushButton_on_start_time > 5): # adds 5sec delay, to prevent immediate switch OFF  
				if float(self.iout_str) <= float(self.i_terminate):         # switch off output
					self.pushButton_onoff.setChecked(False)
					self.pushButton_onoff_clicked()
		elif value == 3:
			self.label_operating_mode.setText('CSV')
		else:
			self.label_operating_mode.setText('Invalid')

	def accrued_capacity(self, current):
		if self.capacity_time_old != '':
			self.capacity_time_current = time.time()
			self.capacity_time_interval = self.capacity_time_current - self.capacity_time_old
			self.capacity_time_old = self.capacity_time_current
			try:
				self.capacity = self.capacity + ((self.capacity_time_interval / 3600.0) * float(current))
			except ZeroDivisionError:
				self.capacity =  0.0
		#	print self.capacity
			self.label_capacity.setText("Capacity   : %8.3fAh" % self.capacity)
		else:
			self.capacity_time_old = time.time()
			
#--- read & display values from DPS 
	def read_all(self):
		data = self.pass_2_dps('read_all')
		if data != False:       
			self.vout_str = f"{data[2]:5.{self.limits.decimals_v}f}"
			self.iout_str = f"{data[3]:5.{self.limits.decimals_i}f}"
			self.vset_str = f"{data[0]:5.{self.limits.decimals_vset}f}"
			self.iset_str = f"{data[1]:5.{self.limits.decimals_iset}f}"
			
			self.accrued_capacity(self.iout_str)
			
			self.time_interval = time.time() - self.time_old			
			self.graph_X.append(self.time_interval)		# Xaxis  - time interval
			self.graph_Y1.append(self.vout_str)				# Y1axis - voltage
			self.graph_Y2.append(self.iout_str)				# Y2axis - current

			self.graph_Y1_set = np.append(self.graph_Y1_set, data[0] * data[18])   # vset * on
			self.graph_Y2_set = np.append(self.graph_Y2_set, data[1] * data[18]) # iset * on
			
			self.update_graph_plot()
			
			self.lcdNumber_vset.display(self.vset_str)  # vset
			self.lcdNumber_iset.display(self.iset_str)  # iset
			self.lcdNumber_vout.display(self.vout_str)  # vout
			self.lcdNumber_iout.display(self.iout_str)  # iout
			self.lcdNumber_temp_internal.display(f"{data[13]:3.{self.limits.decimals_temp_internal}f}")  # temperature internal
			
			self.lcdNumber_pout.display(f"{data[4]:5.{self.limits.decimals_power}f}")  # power
			self.lcdNumber_vin.display(f"{data[5]:5.{self.limits.decimals_vin}f}" )       # vin
		# lock
			value = data[15]
			if value == 1:
				self.radioButton_lock.setChecked(True)
			else:
				self.radioButton_lock.setChecked(False)
				
		# protection  not all values implemented
			value = data[16]
			if value == 1:
				self.label_protect.setText('Protection :   OVP out')
				self.label_led_prot.setPixmap(self.pix_on)
			elif value == 2:
				self.label_protect.setText('Protection :   OCP out')
				self.label_led_prot.setPixmap(self.pix_on)
			elif value == 3:
				self.label_protect.setText('Protection :   OPP out')
				self.label_led_prot.setPixmap(self.pix_on)
			elif value == 4:
				self.label_protect.setText('Protection :   UVP in')
				self.label_led_prot.setPixmap(self.pix_on)
			elif value == 7:
				self.label_protect.setText('Protection :   OTP inter')
				self.label_led_prot.setPixmap(self.pix_on)
			elif value == 0:
				self.label_protect.setText('Protection :   OK')
				self.label_led_prot.setPixmap(self.pix_off)
			else:
				self.label_protect.setText('Protection :   ??')
				self.label_led_prot.setPixmap(self.pix_on)
				
		# temp
			self.label_temp.setText(f'Temperature:  {data[13]:3.{self.limits.decimals_temp_internal}f}')
			
		# energy
			self.label_energy.setText(f'Energy      :    {data[8]:5.{self.limits.decimals_energy}f}Wh')
			
		# time
			self.label_time.setText('Time         :   %3d:%02d:%02d' % (data[10], data[11], data[12]))

		# cv/cc 
			if data[17] == 1:
				self.label_cccv.setText('Mode        :   CC')
				self.label_led.setPixmap(self.pix_on)
			else:
				self.label_cccv.setText('Mode        :   CV')
				self.label_led.setPixmap(self.pix_off)

		# on/off    
			value = data[18]
			if value == 1:
				self.label_onoff.setText('Output      :   ON')  # on/off
				self.pushButton_onoff.setChecked(True)
				self.pushButton_onoff.setText("ON")
			else:
				self.label_onoff.setText('Output      :   OFF') # on/off
				self.pushButton_onoff.setChecked(False)
				self.pushButton_onoff.setText("OFF")

		# # slider    
			# value = int(data[10])
			# self.horizontalSlider_brightness.setValue(value)    # brightness
			# self.label_brightness.setText('Brightness Level:   %s' % value)
			
			self.label_model.setText("Model       :   %x?" % data[22])   # model
			self.label_version.setText("Version     :   %s?" % data[23]) # version

#--- send commands to dps 
	def pass_2_dps(self, function, cmd = "r", value = 0):
		a = False
		if self.serialconnected != False:
			start = time.time()
			self.mutex.lock()
			a = eval("dps.%s('%s', %s)" % (function, cmd, value))
			self.mutex.unlock()
			self.label_data_rate.setText("Data Rate : %8.3fms" % ((time.time() - start) * 1000.0)) # display rate of serial comms
		return(a)
		
#--- serial selection setup       
	def combobox_datarate_read(self):
		return self.connection_settings.get('baudrate', '115200')
					
	def combobox_populate(self):        # collects info on startup		
		if hasattr(self, 'comboBox_datarate'):
			self.comboBox_datarate.clear()
			self.comboBox_datarate.addItems(["115200", "9600", "2400", "4800", "19200"])  # note: 2400 & 19200 doesn't seem to work

#--- serial port stuff  
	def scan_serial_ports(self):
		ports = []
		try:
			ports = sorted(
				port.device
				for port in list_ports.comports()
				if getattr(port, 'device', None)
			)
		except Exception as detail:
			print(datetime.datetime.now().strftime("%y-%m-%d %H:%M:%S"), "Port scan fallback - ", detail)

		if ports:
			return ports

		if sys.platform.startswith('linux') or sys.platform.startswith('cygwin'):
			return glob.glob('/dev/tty[A-Za-z]*')
		if sys.platform.startswith('darwin'):
			return glob.glob('/dev/tty.*')
		return []

	def serial_connect(self, connection_settings = None, progress_callback = None): # port autoconnects, baud rate & slave address manual inputs
		settings = connection_settings or self.connection_settings
		try:
			baudrate = abs(int(settings.get('baudrate', '115200')))
			slave_addr = abs(int(settings.get('slave_addr', '1')))
			selected_port = settings.get('port', '')
			fixed_port = self.limits.port_set or selected_port
			
			candidate_ports = self.scan_serial_ports()
			if not fixed_port: 			# modified by christophjurczyk for automatic port scanning or set serial port
				# Automatic port scan
				print("Looking for ports...")
			else:
				# Manual port definition in .ini file
				print(f"Manual port is set: '{selected_port}'")
				candidate_ports = list([selected_port])
			for port in candidate_ports:
				if not fixed_port:
					print("Trying port: " + port)
				try:
					ser = Serial_modbus(port, slave_addr, baudrate, 8)
					candidate_dps = Dps5005(ser, self.limits) #example '/dev/ttyUSB0', 1, 9600, 8)
					for i in range(2): #try again
						if i>0:
							print(f" Trying again ({i})...")
						version = candidate_dps.version()
						if version not in (False, None, ''):
							candidate_dps.check_model()
							return {
								'connected': True,
								'dps': candidate_dps,
								'connection_settings': {'port': port, 'baudrate': str(baudrate), 'slave_addr': str(slave_addr)},
								'status': "Connected",
							}
				except (OSError, serial.SerialException) as detail1:
					print(datetime.datetime.now().strftime("%y-%m-%d %H:%M:%S"), "Error1 - ", detail1)
					pass

		except Exception as detail:
			print(datetime.datetime.now().strftime("%y-%m-%d %H:%M:%S"), "Error - ", detail)
			return {'connected': False, 'status': "Try again !!!"}

		return {'connected': False, 'status': "Disconnected"}

	def start_serial_connect(self, connection_settings):
		self.connection_in_progress = True
		self.connection_status_text = "Connecting..."
		self.update_connection_button()

		worker = Worker(self.serial_connect, connection_settings)
		worker.signals.result.connect(self.finish_serial_connect)
		worker.signals.error.connect(self.handle_serial_connect_error)
		worker.signals.finished.connect(self.thread_complete)
		self.threadpool.start(worker)

	def finish_serial_connect(self, result):
		self.connection_in_progress = False
		if result and result.get('connected'):
			global dps
			dps = result['dps']
			self.serialconnected = True
			self.connection_settings = result['connection_settings']
			self.connection_status_text = result.get('status', 'Connected')
			self.timer.start()
			if self.time_old == "":
				self.time_old = time.time()
			print([self.connection_settings['port']], self.connection_settings['baudrate'], self.connection_settings['slave_addr'])
			self.pushButton_CSV_view.setEnabled(False)
			self.pushButton_clear_plot_clicked()
		else:
			self.serialconnected = False
			self.connection_status_text = (result or {}).get('status', 'Disconnected')
			self.pushButton_CSV_view.setEnabled(True)
			QMessageBox.warning(self, 'Connection failed', 'Could not connect using the selected serial settings.')
		self.update_connection_button()

	def handle_serial_connect_error(self, error_info):
		self.connection_in_progress = False
		self.serialconnected = False
		self.connection_status_text = "Try again !!!"
		self.pushButton_CSV_view.setEnabled(True)
		self.update_connection_button()
		print(error_info)
		QMessageBox.warning(self, 'Connection failed', 'An unexpected error occurred during serial connection.')
		
	def serial_disconnect(self, status):
		self.shutdown()
		self.connection_in_progress = False
		self.serialconnected = False
		if self.mutex.tryLock():
			self.mutex.unlock()
		self.timer.stop()
		self.connection_status_text = status
		self.update_connection_button()
		self.pushButton_CSV_view.setEnabled(True)						# enable CSV viewing capability
		print(status)
			
app = QApplication(sys.argv)
widget = dps_GUI()
widget.show()

sys.exit(app.exec_())
	
