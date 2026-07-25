package dev.davidv.withoutings

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.core.app.ActivityCompat
import androidx.lifecycle.lifecycleScope
import dev.davidv.withoutings.ui.theme.WithoutingsTheme
import kotlinx.coroutines.launch
import java.util.UUID
import kotlin.reflect.KClass
import kotlin.reflect.full.primaryConstructor
import kotlin.reflect.full.findAnnotation
import java.nio.ByteBuffer
import java.nio.ByteOrder

enum class BleState {
    DISCOVER,
    DISCOVERED,
    CONNECTED,
    SUBSCRIBED
}

enum class ProtocolState {
    UNAUTHENTICATED,
    AUTHENTICATED
}

// Annotations for field types
@Target(AnnotationTarget.VALUE_PARAMETER)
annotation class U8
@Target(AnnotationTarget.VALUE_PARAMETER) 
annotation class U16
@Target(AnnotationTarget.VALUE_PARAMETER)
annotation class U32
@Target(AnnotationTarget.VALUE_PARAMETER)
annotation class U64
@Target(AnnotationTarget.VALUE_PARAMETER)
annotation class Str

// Binary parser
object BinaryParser {
    inline fun <reified T : Any> parse(data: ByteArray): T {
        return parse(data, T::class)
    }
    
    fun <T : Any> parse(data: ByteArray, clazz: KClass<T>): T {
        val buffer = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
        val constructor = clazz.primaryConstructor 
            ?: throw IllegalArgumentException("Class must have a primary constructor")
        
        val args = constructor.parameters.map { param ->
            when {
                param.findAnnotation<U8>() != null -> {
                    buffer.get().toUByte()
                }
                param.findAnnotation<U16>() != null -> {
                    buffer.short.toUShort()
                }
                param.findAnnotation<U32>() != null -> {
                    buffer.int.toUInt()
                }
                param.findAnnotation<U64>() != null -> {
                    buffer.long.toULong()
                }
                param.findAnnotation<Str>() != null -> {
                    // Length-prefixed string: 2 bytes (BE) length + content
                    val length = buffer.short.toInt() and 0xFFFF // unsigned
                    val stringBytes = ByteArray(length)
                    buffer.get(stringBytes)
                    String(stringBytes, Charsets.UTF_8)
                }
                else -> throw IllegalArgumentException("Unsupported type: ${param.type}")
            }
        }
        
        return constructor.call(*args.toTypedArray())
    }
}

// Base command interface
interface Command {
    val commandId: UShort
}

// Example command: Probe
data class CommandProbe(
    @U16 val vid: UShort,
    @U16 val pid: UShort,
    @Str val deviceString: String
) : Command {
    override val commandId: UShort = 0x101u
}

// Registry of all commands
object CommandRegistry {
    private val commands = mapOf<UShort, KClass<out Command>>(
        0x101u to CommandProbe::class
        // Add more commands here
    )
    
    fun getCommandClass(commandId: UShort): KClass<out Command>? = commands[commandId]
    
    fun getAllCommands(): Map<UShort, KClass<out Command>> = commands
    
    fun parseCommand(commandId: UShort, data: ByteArray): Command? {
        val commandClass = getCommandClass(commandId) ?: return null
        return try {
            BinaryParser.parse(data, commandClass)
        } catch (e: Exception) {
            Log.e("PARSER", "Failed to parse command $commandId: ${e.message}")
            null
        }
    }
}

// Parsed message structure
data class ParsedMessage(
    val protocolVersion: UByte,
    val messageCommand: UShort,
    val commands: List<Command>
)

// Message parser using reflection-based binary parser
object MessageParser {
    fun parse(data: ByteArray): ParsedMessage? {
        try {
            Log.d("PARSER", "Parsing ${data.size} bytes: ${data.joinToString("") { "%02x".format(it) }}")
            
            if (data.size < 5) {
                Log.e("PARSER", "Message too short for header")
                return null
            }
            
            val buffer = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN)
            
            // Parse header: version (1) + command (2) + payload length (2)
            val protocolVersion = buffer.get().toUByte()
            val messageCommand = buffer.short.toUShort()
            val payloadLength = buffer.short.toUShort()
            
            Log.d("PARSER", "Header: version=$protocolVersion, command=$messageCommand, payloadLength=$payloadLength")
            
            // Parse commands from payload
            val commands = parseCommands(buffer, payloadLength.toInt())
            
            return ParsedMessage(protocolVersion, messageCommand, commands)
        } catch (e: Exception) {
            Log.e("PARSER", "Failed to parse message: ${e.message}")
            return null
        }
    }
    
    private fun parseCommands(buffer: ByteBuffer, payloadLength: Int): List<Command> {
        val commands = mutableListOf<Command>()
        val payloadStart = buffer.position()
        
        while (buffer.position() - payloadStart < payloadLength && buffer.remaining() >= 4) {
            try {
                // Parse command: id (2) + length (2) + data
                val commandId = buffer.short.toUShort()
                val commandLength = buffer.short.toUShort()
                
                if (buffer.remaining() < commandLength.toInt()) {
                    Log.e("PARSER", "Not enough data for command $commandId (need ${commandLength}, have ${buffer.remaining()})")
                    break
                }
                
                val commandData = ByteArray(commandLength.toInt())
                buffer.get(commandData)
                
                Log.d("PARSER", "Parsing command $commandId with ${commandLength} bytes")
                
                val command = CommandRegistry.parseCommand(commandId, commandData)
                if (command != null) {
                    commands.add(command)
                    Log.d("PARSER", "Successfully parsed command: $command")
                } else {
                    Log.w("PARSER", "Unknown or failed to parse command $commandId")
                }
            } catch (e: Exception) {
                Log.e("PARSER", "Error parsing command: ${e.message}")
                break
            }
        }
        
        return commands
    }
}

data class BleDevice(
    val device: BluetoothDevice,
    val rssi: Int,
    val name: String = device.name ?: "Unknown Device",
    val isConnected: Boolean = false,
    val source: String = "Scanned"
)

class MainActivity : ComponentActivity() {
    private lateinit var bluetoothAdapter: BluetoothAdapter
    private lateinit var bluetoothLeScanner: BluetoothLeScanner
    private var bluetoothGatt: BluetoothGatt? = null
    private var isScanning = false
    private val discoveredDevices = mutableStateListOf<BleDevice>()
    private var connectedDeviceAddress by mutableStateOf<String?>(null)
    private var handle4Characteristic: BluetoothGattCharacteristic? = null
    private var currentBleState by mutableStateOf(BleState.DISCOVER)
    private var currentDevice: BluetoothDevice? = null
    private var currentProtocolState by mutableStateOf(ProtocolState.UNAUTHENTICATED)
    
    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { permissions ->
        val allGranted = permissions.all { it.value }
        if (allGranted) {
            Log.d("BLE", "All permissions granted")
        } else {
            Log.e("BLE", "Permissions denied")
        }
    }
    
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        
        initializeBluetooth()
        lifecycleScope.launch {
            loadConnectedAndBondedDevices()
        }

        setContent {
            WithoutingsTheme {
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    BleScreen(
                        modifier = Modifier.padding(innerPadding),
                        onStartScan = { startBleScan() },
                        isScanning = isScanning,
                        devices = discoveredDevices,
                        onDeviceClick = { device -> connectToDevice(device.device) },
                        connectedDeviceAddress = connectedDeviceAddress,
                        currentState = currentBleState,
                        protocolState = currentProtocolState
                    )
                }
            }
        }
    }
    
    private fun initializeBluetooth() {
        val bluetoothManager = getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        bluetoothAdapter = bluetoothManager.adapter
        
        if (!bluetoothAdapter.isEnabled) {
            Log.e("BLE", "Bluetooth is not enabled")
            return
        }
        
        bluetoothLeScanner = bluetoothAdapter.bluetoothLeScanner
        requestBlePermissions()
    }
    
    private fun requestBlePermissions() {
        val permissions = arrayOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION
        )
        
        val needsPermission = permissions.any { 
            ActivityCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED 
        }
        
        if (needsPermission) {
            requestPermissionLauncher.launch(permissions)
        }
    }
    
    private fun loadConnectedAndBondedDevices() {
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
            Log.e("BLE", "BLUETOOTH_CONNECT permission not granted for loading bonded devices")
            return
        }
        
        val bluetoothManager = getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        val connectedDevices = bluetoothManager.getConnectedDevices(BluetoothProfile.GATT)
        
        Log.d("BLE", "Found ${connectedDevices.size} connected GATT devices")
        
        for (device in connectedDevices) {
            val macAddress = device.address
            if (macAddress.startsWith("A4:7E:FA") || macAddress.startsWith("CF:89")) {
                val bleDevice = BleDevice(
                    device = device,
                    rssi = 0,
                    isConnected = true,
                    source = "Connected"
                )
                discoveredDevices.add(bleDevice)
                Log.d("BLE", "Added connected device: ${bleDevice.name} - ${device.address}")
                
                // If we find a connected device, set it as current and transition to CONNECTED state
                // Note: We don't have the GATT reference here, so we'll need to connect to get it
                if (currentBleState == BleState.DISCOVER) {
                    currentDevice = device
                    connectedDeviceAddress = device.address
                    // We need to connect to get the GATT reference for service operations
                    connectToDevice(device)
                }
            }
        }
        
        val bondedDevices = bluetoothAdapter.bondedDevices
        Log.d("BLE", "Found ${bondedDevices.size} bonded devices")
        
        for (device in bondedDevices) {
            val macAddress = device.address
            if (macAddress.startsWith("A4:7E:FA") || macAddress.startsWith("CF:89")) {
                if (discoveredDevices.none { it.device.address == device.address }) {
                    val bleDevice = BleDevice(
                        device = device,
                        rssi = 0,
                        source = "Bonded"
                    )
                    discoveredDevices.add(bleDevice)
                    Log.d("BLE", "Added bonded device: ${bleDevice.name} - ${device.address}")
                }
            }
        }
    }
    
    private fun startBleScan() {
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED) {
            Log.e("BLE", "BLUETOOTH_SCAN permission not granted")
            return
        }
        
        if (isScanning) {
            bluetoothLeScanner.stopScan(scanCallback)
            isScanning = false
            Log.d("BLE", "Stopped BLE scan")
        } else {
            discoveredDevices.clear()
            bluetoothLeScanner.startScan(scanCallback)
            isScanning = true
            Log.d("BLE", "Started BLE scan")
        }
    }
    
    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            super.onScanResult(callbackType, result)
            
            val device = result.device
            val macAddress = device.address

            // A4 == withings
            if (macAddress.startsWith("A4:7E:FA") || macAddress.startsWith("CF:89")) {
                val bleDevice = BleDevice(
                    device = device, 
                    rssi = result.rssi,
                    source = "Scanned"
                )
                
                if (discoveredDevices.none { it.device.address == device.address }) {
                    discoveredDevices.add(bleDevice)
                    Log.d("BLE", "Found device: ${bleDevice.name} - ${device.address} (RSSI: ${result.rssi})")
                }
            }
        }
        
        override fun onScanFailed(errorCode: Int) {
            super.onScanFailed(errorCode)
            Log.e("BLE", "Scan failed with error code: $errorCode")
            isScanning = false
        }
    }
    
    private fun connectToDevice(device: BluetoothDevice) {
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
            Log.e("BLE", "BLUETOOTH_CONNECT permission not granted")
            return
        }
        
        bluetoothGatt?.close()
        
        Log.d("BLE", "Connecting to device: ${device.name ?: "Unknown"}")
        bluetoothGatt = device.connectGatt(this, false, gattCallback)
    }
    
    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            super.onConnectionStateChange(gatt, status, newState)
            
            when (newState) {
                BluetoothProfile.STATE_CONNECTED -> {
                    Log.d("BLE", "Connected to GATT server")
                    connectedDeviceAddress = gatt.device.address
                    currentDevice = gatt.device
                    onStateChanged(BleState.CONNECTED)
                }
                BluetoothProfile.STATE_DISCONNECTED -> {
                    Log.d("BLE", "Disconnected from GATT server")
                    onStateChanged(BleState.DISCOVER)
                }
            }
        }
        
        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            super.onServicesDiscovered(gatt, status)
            
            if (status == BluetoothGatt.GATT_SUCCESS) {
                Log.d("BLE", "Services discovered successfully")
                onStateChanged(BleState.DISCOVERED)
            } else {
                Log.e("BLE", "Service discovery failed with status: $status")
            }
        }
        
        override fun onCharacteristicChanged(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray) {
            super.onCharacteristicChanged(gatt, characteristic, value)
            Log.d("BLE", "Notification received from ${characteristic.uuid}")
            Log.d("BLE", "Data: ${value.joinToString(" ") { "%02x".format(it) }}")
            
            // Handle protocol messages based on current state
            handleProtocolMessage(value)
        }
        
        override fun onCharacteristicWrite(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) {
            super.onCharacteristicWrite(gatt, characteristic, status)
            Log.d("BLE", "Characteristic write completed with status: $status")
            if (status == BluetoothGatt.GATT_SUCCESS) {
                Log.d("BLE", "Write successful to ${characteristic.uuid}")
            } else {
                Log.e("BLE", "Write failed to ${characteristic.uuid} with status: $status")
            }
        }
        
        override fun onDescriptorWrite(gatt: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            super.onDescriptorWrite(gatt, descriptor, status)
            Log.d("BLE", "Descriptor write completed with status: $status")
            if (status == BluetoothGatt.GATT_SUCCESS) {
                Log.d("BLE", "Descriptor write successful - notifications enabled")
                onStateChanged(BleState.SUBSCRIBED)
            } else {
                Log.e("BLE", "Descriptor write failed with status: $status")
            }
        }
    }
    
    private fun onStateChanged(newState: BleState) {
        Log.d("BLE", "State transition: ${currentBleState} -> $newState")
        currentBleState = newState
        
        when (newState) {
            BleState.DISCOVER -> {
                // Reset state when returning to discover
                handle4Characteristic = null
                connectedDeviceAddress = null
                currentDevice = null
                currentProtocolState = ProtocolState.UNAUTHENTICATED
            }
            BleState.CONNECTED -> {
                // Start service discovery when connected
                bluetoothGatt?.let { gatt ->
                    if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED) {
                        gatt.discoverServices()
                    }
                }
            }
            BleState.DISCOVERED -> {
                // Find and subscribe to handle 4 when services are discovered
                bluetoothGatt?.let { gatt ->
                    findAndSubscribeToHandle4(gatt)
                }
            }
            BleState.SUBSCRIBED -> {
                // Start protocol state machine when subscribed
                onProtocolStateChanged(ProtocolState.UNAUTHENTICATED)
            }
        }
    }
    
    private fun handleProtocolMessage(data: ByteArray) {
        val hexData = data.joinToString("") { "%02x".format(it) }
        Log.d("PROTOCOL", "Processing message in state ${currentProtocolState}: $hexData")
        
        // Try to parse the message using the new framework
        val parsedMessage = MessageParser.parse(data)
        
        if (parsedMessage != null) {
            Log.d("PROTOCOL", "Parsed message: protocol=${parsedMessage.protocolVersion}, command=${parsedMessage.messageCommand}, ${parsedMessage.commands.size} commands")
            
            // Handle parsed commands
            parsedMessage.commands.forEach { command ->
                handleCommand(command)
            }
        } else {
            // Fallback to old parsing logic until binary parser is implemented
            Log.d("PROTOCOL", "Using fallback message handling")
            handleUnparsedMessage(data)
        }
    }
    
    private fun handleCommand(command: Command) {
        Log.d("PROTOCOL", "Handling command: ${command.commandId}")
        
        when (command) {
            is CommandProbe -> {
                Log.d("PROTOCOL", "Probe command: vid=${command.vid}, pid=${command.pid}, device='${command.deviceString}'")
                // Handle probe command based on current state
                when (currentProtocolState) {
                    ProtocolState.UNAUTHENTICATED -> {
                        // Probe response received, transition to authenticated
                        onProtocolStateChanged(ProtocolState.AUTHENTICATED)
                    }
                    ProtocolState.AUTHENTICATED -> {
                        // Handle probe in authenticated state
                        Log.d("PROTOCOL", "Probe received while authenticated")
                    }
                }
            }
            else -> {
                Log.d("PROTOCOL", "Unknown command type: ${command.commandId}")
            }
        }
    }
    
    private fun handleUnparsedMessage(data: ByteArray) {
        // Legacy handling until binary parser is ready
        when (currentProtocolState) {
            ProtocolState.UNAUTHENTICATED -> {
                Log.d("PROTOCOL", "Received message while UNAUTHENTICATED - assuming auth response")
                // For now, assume any response means authentication succeeded
                onProtocolStateChanged(ProtocolState.AUTHENTICATED)
            }
            ProtocolState.AUTHENTICATED -> {
                Log.d("PROTOCOL", "Received message while AUTHENTICATED - processing as data")
            }
        }
    }
    
    private fun onProtocolStateChanged(newState: ProtocolState) {
        Log.d("PROTOCOL", "Protocol state transition: ${currentProtocolState} -> $newState")
        currentProtocolState = newState
        
        when (newState) {
            ProtocolState.UNAUTHENTICATED -> {
                // Send probe message when entering unauthenticated state
                sendProbe()
            }
            ProtocolState.AUTHENTICATED -> {
                // TODO: Handle authenticated state actions
                Log.d("PROTOCOL", "Device is now authenticated")
            }
        }
    }
    
    private fun sendProbe() {
        bluetoothGatt?.let { gatt ->
            handle4Characteristic?.let { characteristic ->
                Log.d("PROTOCOL", "Sending probe message")
                val hexString = "0101010010012a00060101006b93d9092800020023"
                val data = hexString.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
                
                // Log the framework status
                logFrameworkStatus()
                
                writeToHandle4(gatt, characteristic, data)
            }
        }
    }
    
    private fun logFrameworkStatus() {
        Log.d("FRAMEWORK", "=== Message Parsing Framework Status ===")
        Log.d("FRAMEWORK", "Registered commands: ${CommandRegistry.getAllCommands().size}")
        
        CommandRegistry.getAllCommands().forEach { (id, commandClass) ->
            Log.d("FRAMEWORK", "Command 0x${id.toString(16)}: ${commandClass.simpleName}")
            
            // Log constructor parameters with annotations
            commandClass.primaryConstructor?.parameters?.forEach { param ->
                val annotation = when {
                    param.findAnnotation<U8>() != null -> "@U8"
                    param.findAnnotation<U16>() != null -> "@U16"
                    param.findAnnotation<U32>() != null -> "@U32"
                    param.findAnnotation<U64>() != null -> "@U64"
                    param.findAnnotation<Str>() != null -> "@Str"
                    else -> "Unknown"
                }
                Log.d("FRAMEWORK", "  - ${param.name}: $annotation")
            }
        }
        
        Log.d("FRAMEWORK", "Current protocol state: $currentProtocolState")
        Log.d("FRAMEWORK", "Binary parser: REFLECTION-BASED IMPLEMENTATION")
        Log.d("FRAMEWORK", "==========================================")
    }
    
    private fun findAndSubscribeToHandle4(gatt: BluetoothGatt) {
        val services = gatt.services
        val filteredServices = services.filter { it.uuid.toString().contains("5749-5448") }
        
        Log.d("BLE", "Found ${services.size} total services, ${filteredServices.size} matching services")
        
        for (service in filteredServices) {
            val characteristics = service.characteristics
            val filteredCharacteristics = characteristics.filter { it.uuid.toString().uppercase().contains("494E4753") }
            
            Log.d("BLE", "Found ${characteristics.size} total characteristics, ${filteredCharacteristics.size} matching characteristics")
            
            for (characteristic in filteredCharacteristics) {
                val uuidString = characteristic.uuid.toString()
                val handleId = try {
                    uuidString[7].toString().toInt()
                } catch (e: Exception) {
                    Log.e("BLE", "Failed to extract handle ID from UUID: $uuidString")
                    -1
                }
                
                Log.d("BLE", "Characteristic UUID: ${characteristic.uuid}")
                Log.d("BLE", "Handle ID: $handleId")
                Log.d("BLE", "Properties: ${getCharacteristicProperties(characteristic)}")

                if (handleId == 4) { // 4 gotten from packet dumps
                    handle4Characteristic = characteristic
                    registerForNotifications(gatt, characteristic)
                    return
                }
            }
        }
        
        Log.e("BLE", "Handle 4 characteristic not found")
    }
    
    private fun registerForNotifications(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
            Log.e("BLE", "BLUETOOTH_CONNECT permission not granted for notifications")
            return
        }
        
        val success = gatt.setCharacteristicNotification(characteristic, true)
        Log.d("BLE", "Setting notification for handle 4: $success")
        
        val descriptor = characteristic.getDescriptor(UUID.fromString("00002902-0000-1000-8000-00805f9b34fb"))
        if (descriptor != null) {
            val descriptorWrite = gatt.writeDescriptor(descriptor, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
            Log.d("BLE", "Writing notification descriptor: $descriptorWrite")
        } else {
            Log.e("BLE", "Client Characteristic Configuration descriptor not found")
        }
    }
    
    private fun writeToHandle4(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic, data: ByteArray) {
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
            Log.e("BLE", "BLUETOOTH_CONNECT permission not granted for write")
            return
        }
        
        val properties = characteristic.properties
        Log.d("BLE", "Characteristic properties: ${getCharacteristicProperties(characteristic)}")
        
        if (properties and BluetoothGattCharacteristic.PROPERTY_WRITE == 0 && 
            properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE == 0) {
            Log.e("BLE", "Characteristic does not support write operations")
            return
        }
        

        
        val writeType = if (properties and BluetoothGattCharacteristic.PROPERTY_WRITE != 0) {
            BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        } else {
            BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        }
        
        Log.d("BLE", "Using write type: $writeType")
        val writeSuccess = gatt.writeCharacteristic(characteristic, data, writeType)
        Log.d("BLE", "Writing to handle 4: $writeSuccess")
        Log.d("BLE", "Data written: ${data.joinToString(" ") { "%02x".format(it) }}")
    }
    
    
    private fun getCharacteristicProperties(characteristic: BluetoothGattCharacteristic): String {
        val properties = mutableListOf<String>()
        val props = characteristic.properties
        
        if (props and BluetoothGattCharacteristic.PROPERTY_READ != 0) properties.add("READ")
        if (props and BluetoothGattCharacteristic.PROPERTY_WRITE != 0) properties.add("WRITE")
        if (props and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE != 0) properties.add("WRITE_NO_RESPONSE")
        if (props and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0) properties.add("NOTIFY")
        
        return properties.joinToString(", ")
    }

    
    override fun onDestroy() {
        super.onDestroy()
        bluetoothGatt?.close()
        if (isScanning && ActivityCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED) {
            bluetoothLeScanner.stopScan(scanCallback)
        }
    }
}

@Composable
fun BleScreen(
    modifier: Modifier = Modifier,
    onStartScan: () -> Unit,
    isScanning: Boolean,
    devices: List<BleDevice>,
    onDeviceClick: (BleDevice) -> Unit,
    connectedDeviceAddress: String?,
    currentState: BleState,
    protocolState: ProtocolState
) {
    Column(
        modifier = modifier.fillMaxSize().padding(16.dp)
    ) {
        Text(
            text = "BLE Device Scanner",
            style = MaterialTheme.typography.headlineMedium,
            modifier = Modifier.padding(bottom = 16.dp)
        )
        
        Text(
            text = "BLE State: ${currentState.name}",
            style = MaterialTheme.typography.titleMedium,
            color = when (currentState) {
                BleState.DISCOVER -> MaterialTheme.colorScheme.onSurface
                BleState.DISCOVERED -> MaterialTheme.colorScheme.primary
                BleState.CONNECTED -> MaterialTheme.colorScheme.secondary
                BleState.SUBSCRIBED -> MaterialTheme.colorScheme.tertiary
            },
            modifier = Modifier.padding(bottom = 8.dp)
        )
        
        if (currentState == BleState.SUBSCRIBED) {
            Text(
                text = "Protocol: ${protocolState.name}",
                style = MaterialTheme.typography.titleMedium,
                color = when (protocolState) {
                    ProtocolState.UNAUTHENTICATED -> MaterialTheme.colorScheme.error
                    ProtocolState.AUTHENTICATED -> MaterialTheme.colorScheme.primary
                },
                modifier = Modifier.padding(bottom = 16.dp)
            )
        } else {
            Modifier.padding(bottom = 16.dp)
        }
        
        Button(
            onClick = onStartScan,
            modifier = Modifier.fillMaxWidth().padding(bottom = 16.dp)
        ) {
            Text(if (isScanning) "Stop Scan" else "Start Scan")
        }
        
        Text(
            text = when {
                currentState == BleState.SUBSCRIBED && protocolState == ProtocolState.AUTHENTICATED -> "Authenticated and ready"
                currentState == BleState.SUBSCRIBED && protocolState == ProtocolState.UNAUTHENTICATED -> "Authenticating..."
                currentState == BleState.SUBSCRIBED -> "Connected and subscribed to handle 4"
                currentState == BleState.CONNECTED -> "Connected - discovering services..."
                currentState == BleState.DISCOVERED -> "Services discovered - subscribing..."
                isScanning -> "Scanning for devices..."
                devices.isEmpty() -> "No devices found. Press button to scan."
                else -> "Found ${devices.size} device(s). Tap to connect:"
            },
            modifier = Modifier.padding(bottom = 16.dp)
        )
        
        LazyColumn {
            items(devices) { bleDevice ->
                DeviceCard(
                    device = bleDevice,
                    isConnected = bleDevice.device.address == connectedDeviceAddress,
                    onClick = { onDeviceClick(bleDevice) }
                )
            }
        }
    }
}

@Composable
fun DeviceCard(
    device: BleDevice,
    isConnected: Boolean,
    onClick: () -> Unit
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .clickable { onClick() },
        colors = CardDefaults.cardColors(
            containerColor = if (isConnected) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surface
        )
    ) {
        Column(
            modifier = Modifier.padding(16.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    text = device.name,
                    style = MaterialTheme.typography.titleMedium
                )
                Text(
                    text = if (device.rssi != 0) "${device.rssi} dBm" else device.source,
                    style = MaterialTheme.typography.bodySmall
                )
            }
            Text(
                text = device.device.address,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.padding(top = 4.dp)
            )
            if (isConnected) {
                Text(
                    text = "Connected - Check logcat for services",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(top = 4.dp)
                )
            } else if (device.source != "Scanned") {
                Text(
                    text = device.source,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.secondary,
                    modifier = Modifier.padding(top = 4.dp)
                )
            }
        }
    }
}

@Preview(showBackground = true)
@Composable
fun BleScreenPreview() {
    WithoutingsTheme {
        BleScreen(
            onStartScan = {},
            isScanning = false,
            devices = emptyList(),
            onDeviceClick = {},
            connectedDeviceAddress = null,
            currentState = BleState.DISCOVER,
            protocolState = ProtocolState.UNAUTHENTICATED
        )
    }
}