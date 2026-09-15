//
// Keyboard-to-GPIO bridge for the video analyzer window. Renode's VideoAnalyzer
// forwards window key events to any IKeyboard in the same machine (it lists
// them in the "Keyboard:" combo box next to the frame), and that is the only
// input path from the display window into the simulation. The watch has no
// keyboard, so this model turns a fixed list of keys into GPIO lines that the
// input peripherals (crown, charger detect, buttons) can be wired to in the
// platform description, e.g.
//
//   keys: Input.KeyPadInput @ sysbus
//       keys: ["Up", "Down", "Enter"]
//       0 -> crown@0
//       preinit:
//           include @KeyInput.cs
//
// The registration point is only what makes the monitor and the analyzer see
// the peripheral; this model has no bus interface.
//
// The same lines can be driven from the monitor without the window, with
// `sysbus.keys Tap "Up"`, which is what an external UI over `renode --port`
// uses.
//
using System;
using System.Collections.Generic;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Peripherals;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;

namespace Antmicro.Renode.Peripherals.Input
{
    public class KeyPadInput : IKeyboard, INumberedGPIOOutput
    {
        public KeyPadInput(string[] keys)
        {
            if(keys.Length == 0)
            {
                throw new ConstructionException("at least one key is needed, each one becomes a GPIO line");
            }
            lines = new Dictionary<KeyScanCode, GPIO>();
            var connections = new Dictionary<int, IGPIO>();
            for(var i = 0; i < keys.Length; i++)
            {
                KeyScanCode code;
                if(!Enum.TryParse(keys[i], out code))
                {
                    throw new ConstructionException(string.Format("'{0}' is not a KeyScanCode name", keys[i]));
                }
                if(lines.ContainsKey(code))
                {
                    throw new ConstructionException(string.Format("key '{0}' is listed twice", keys[i]));
                }
                var line = new GPIO();
                lines.Add(code, line);
                connections.Add(i, line);
            }
            Connections = connections;
        }

        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        public void Press(KeyScanCode scanCode)
        {
            Drive(scanCode, true);
        }

        public void Release(KeyScanCode scanCode)
        {
            Drive(scanCode, false);
        }

        // A press and release in one monitor command: a key event from outside
        // the emulation has no held duration to reproduce.
        public void Tap(string key)
        {
            KeyScanCode code;
            if(!Enum.TryParse(key, out code))
            {
                throw new RecoverableException(string.Format("'{0}' is not a KeyScanCode name", key));
            }
            Drive(code, true);
            Drive(code, false);
        }

        public void Reset()
        {
            foreach(var line in lines.Values)
            {
                line.Unset();
            }
        }

        public IEnumerable<string> Keys
        {
            get { return lines.Keys.Select(k => k.ToString()); }
        }

        private void Drive(KeyScanCode scanCode, bool state)
        {
            GPIO line;
            if(!lines.TryGetValue(scanCode, out line))
            {
                return;
            }
            if(!this.TryGetMachine(out var machine))
            {
                line.Set(state);
                return;
            }
            machine.HandleTimeDomainEvent(line.Set, state, TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp());
        }

        private readonly Dictionary<KeyScanCode, GPIO> lines;
    }
}
