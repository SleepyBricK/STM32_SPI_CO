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
% (may be different than COM6 on your system)
device = serialport("COM6", 6000000);

% Check for already present data on the port
preExistingBytes = device.NumBytesAvailable;
if preExistingBytes ~= 0
    read(device, preExistingBytes, "uint8");
end

total_samples = TIMESTAMPS_TO_PLOT*SAMPLED_CHANNELS;

while 1
    if device.NumBytesAvailable >= total_samples*4
        
        % Read 32-bit data from port
        readData = uint32(read(device, total_samples, "uint32"));
        
        % Separate each 32-bit word into AC and DC data
        readACWords = uint16(bitshift(readData, -16));
        readDCWords = uint16(bitand(readData, 65535));
        
        % Scale AC and DC data
        readACData = 0.195 * (double(readACWords) - 32768);
        readDCData = -0.01923 * (double(readDCWords) - 512);
        
        for i=1:SAMPLED_CHANNELS
            % Plot AC
            subplot(SAMPLED_CHANNELS*2,2,2*i-1);
            plot(readACData(i:SAMPLED_CHANNELS:end));
            title(['AC channel: ', num2str(i)]);
            
            % Plot DC
            subplot(SAMPLED_CHANNELS*2,2,2*i);
            plot(readDCData(i:SAMPLED_CHANNELS:end));
            title(['DC channel: ', num2str(i)]);
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