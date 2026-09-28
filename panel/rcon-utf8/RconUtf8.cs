// The RCON mod (AviiNL-rcon) reads every command with Encoding.ASCII, so each Polish letter the
// panel sends arrives in the game as '?'. This plugin swaps that one call for Encoding.UTF8 at
// load time (a Harmony transpiler on the mod's receive method). The panel already sends UTF-8,
// and the packet length is counted in bytes, so nothing else has to change.
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using BepInEx;
using HarmonyLib;

[BepInPlugin("eu.klans.valheim_proxmox.rcon_utf8", "rcon UTF-8", "1.0.0")]
[BepInDependency("nl.avii.plugins.rcon")]
public class RconUtf8 : BaseUnityPlugin
{
    static readonly MethodInfo Ascii = AccessTools.PropertyGetter(typeof(Encoding), nameof(Encoding.ASCII));
    static readonly MethodInfo Utf8 = AccessTools.PropertyGetter(typeof(Encoding), nameof(Encoding.UTF8));
    static int swapped;

    void Awake()
    {
        var read = AccessTools.Method(AccessTools.TypeByName("rcon.Internal.AsynchronousSocketListener"), "ReadCallback");
        if (read == null)
        {
            Logger.LogWarning("the rcon mod's receive method was not found - messages stay ASCII");
            return;
        }
        new Harmony("eu.klans.valheim_proxmox.rcon_utf8").Patch(read,
            transpiler: new HarmonyMethod(typeof(RconUtf8).GetMethod(nameof(ToUtf8), BindingFlags.Static | BindingFlags.NonPublic)));
        Logger.LogInfo(swapped > 0 ? "rcon reads UTF-8" : "rcon: no ASCII call found - left as it was");
    }

    static IEnumerable<CodeInstruction> ToUtf8(IEnumerable<CodeInstruction> code)
    {
        foreach (var c in code)
        {
            if (c.Calls(Ascii)) { c.operand = Utf8; swapped++; }
            yield return c;
        }
    }
}
