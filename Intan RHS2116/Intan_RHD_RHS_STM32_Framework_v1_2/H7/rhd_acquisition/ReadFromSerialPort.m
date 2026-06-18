function ReadFromSerialPort

% If received data looks corrupted, try to start running this function with
% the black RESET button on the NUCLEO board held down, then release RESET
% button.
SAMPLED_CHANNELS = 4;
NUM_SECONDS_TO_PLOT = 0.5;
SAMPLE_RATE = 20000;
TIMESTAMPS_TO_PLOT = SAMPLE_RATE * NUM_SECONDS_TO_PLOT;

OFFLINE_TRANSFER = true;

% Connect to Serial Port that STM32H7 is on
% (may be different than COM5 on your system)
device = serialport("COM5", 12500000);

% Check for already present data on the port
preExistingBytes = device.NumBytesAvailable;
if preExistingBytes ~= 0
    read(device, preExistingBytes, "uint8");
end

total_samples = TIMESTAMPS_TO_PLOT*SAMPLED_CHANNELS;

while 1
    if device.NumBytesAvailable >= total_samples*2
        readData = 0.195 * (read(device, total_samples, "uint16") - 32768);
        
        for i=1:SAMPLED_CHANNELS
            subplot(SAMPLED_CHANNELS,1,i);
            plot(readData(i:SAMPLED_CHANNELS:end));
            title(['Received channel index: ', num2str(i)]);
        end
        drawnow;

        if OFFLINE_TRANSFER
            % Check for unexpected remaining bytes.
            remainderBytes = device.NumBytesAvailable;
            if (remainderBytes ~= 0)
                fprintf(1, 'ERROR Non-zero number of remaining bytes: %d\n', remainderBytes);
                remainderData = read(device, remainderBytes, "uint8");
            end
            delete(device);
            break
        end
    end
end
end