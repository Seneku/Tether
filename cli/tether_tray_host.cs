// Tether.exe: runs tether_tray.ps1 inside its own process, so Windows lists the notification-area icon as
// "Tether" (Settings > Personalisation > Taskbar > Other system tray icons) rather than "Windows PowerShell".
// `ue dash --tray` compiles it with the .NET Framework's csc.exe into cli/Tether.exe, next to ue.py.
// Usage: Tether.exe <tether_tray.ps1> [-Name value ...]
using System;
using System.IO;
using System.Management.Automation;
using System.Management.Automation.Runspaces;
using System.Reflection;

[assembly: AssemblyTitle("Tether")]
[assembly: AssemblyProduct("Tether")]
[assembly: AssemblyDescription("Tether tray icon for ue dash")]

static class TetherTrayHost {
    [STAThread]
    static int Main(string[] args) {
        if (args.Length < 1) return 2;
        string log = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".tether", "tray-host.log");
        try {
            InitialSessionState state = InitialSessionState.CreateDefault();
            state.ApartmentState = System.Threading.ApartmentState.STA;
            state.ThreadOptions = PSThreadOptions.UseCurrentThread;
            using (Runspace runspace = RunspaceFactory.CreateRunspace(state)) {
                runspace.ApartmentState = System.Threading.ApartmentState.STA;
                runspace.ThreadOptions = PSThreadOptions.UseCurrentThread;
                runspace.Open();
                using (PowerShell shell = PowerShell.Create()) {
                    shell.Runspace = runspace;
                    // The script's text as a script block: no execution-policy check, and its param() binds below.
                    shell.AddScript(File.ReadAllText(args[0]));
                    for (int i = 1; i + 1 < args.Length; i += 2) shell.AddParameter(args[i].TrimStart('-'), args[i + 1]);
                    shell.Invoke();
                    foreach (ErrorRecord e in shell.Streams.Error) File.AppendAllText(log, DateTime.Now + " " + e + Environment.NewLine);
                }
            }
            return 0;
        } catch (Exception e) {
            try { File.AppendAllText(log, DateTime.Now + " " + e + Environment.NewLine); } catch { }
            return 1;
        }
    }
}
