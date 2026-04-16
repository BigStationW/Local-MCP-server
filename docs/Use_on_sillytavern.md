# Installation
Assuming that Local-MCP-server is active (by double clicking on launch.bat)

- Navigate to the **SillyTavern\plugins** folder, [open cmd](https://www.youtube.com/watch?v=bgSSJQolR0E&t=47s) and run:

```
git clone https://github.com/bmen25124/SillyTavern-MCP-Server
```

- Navigate to the **SillyTavern** folder and open **config.yaml** with a notepad

On the bottom of the file, change ```enableServerPlugins: false``` to ```enableServerPlugins: true```

<img width="300" alt="image" src="https://github.com/user-attachments/assets/8e0ac24b-83df-4d7a-a01b-93ca2d597f27" />

- Run SillyTavern and go to its frontend web page
- Go to **API Connections** -> **Chat completion**

 <img width="300" alt="image" src="https://github.com/user-attachments/assets/a7a96d53-6879-4c02-bb34-a8e344ff11f9" />
 

 - Go to **API Response Configuration** -> check **Enable function calling**

<img width="300" alt="image" src="https://github.com/user-attachments/assets/9ce3543d-c146-4426-a132-a9892e993360" />


- Go to **User Settings** -> uncheck **Forbid External Media**

<img width="300" alt="image" src="https://github.com/user-attachments/assets/0057f885-79f4-4f35-b7ca-cbdb69a6f3b5" />

- Go to **Extensions** -> **Install Extension** -> Paste the link below and click on **Install just for me**:
```https://github.com/bmen25124/SillyTavern-MCP-Client```

- Go to **Extensions** -> **MCP Settings** -> check **Enable MCP**
- [Go to **Extensions** -> **MCP Settings** -> **Manage Tools** -> **+ Add Server** and paste this:](https://github.com/bmen25124/SillyTavern-MCP-Client?tab=readme-ov-file#demo)
```
{
  "mcpServers": {
    "name": {
      "url": "http://localhost:4242/mcp",
      "type": "streamableHttp"
    }
  }
}
```

# Example

<img width="700" alt="image" src="https://github.com/user-attachments/assets/552f24bc-06f6-49e5-a7ad-46174a4482d7" />
