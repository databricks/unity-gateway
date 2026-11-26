using System;
using System.Collections.Generic;
using System.Windows.Automation;

namespace WindowsUIA
{
    public sealed class ComposerSample
    {
        public string timestamp { get; set; }
        public string value { get; set; }
        public int[] runtime_id { get; set; }
    }

    public sealed class ComposerProbe : IDisposable
    {
        private readonly List<AutomationElement> roots = new List<AutomationElement>();
        private readonly List<ComposerSample> samples = new List<ComposerSample>();
        private readonly Dictionary<string, int> lengths = new Dictionary<string, int>();
        private readonly AutomationPropertyChangedEventHandler handler;
        private int errors;

        public ComposerProbe()
        {
            handler = Changed;
        }

        public void Observe(AutomationElement root)
        {
            Automation.AddAutomationPropertyChangedEventHandler(
                root, TreeScope.Subtree, handler, ValuePattern.ValueProperty);
            roots.Add(root);
        }

        private void Changed(object sender, AutomationPropertyChangedEventArgs args)
        {
            var element = sender as AutomationElement;
            var value = args.NewValue as string;
            if (element == null || value == null)
                return;
            try
            {
                if (element.Current.ControlType != ControlType.Edit)
                    return;
                var id = element.GetRuntimeId();
                var key = string.Join(",", id);
                lock (samples)
                {
                    int previous;
                    bool seen = lengths.TryGetValue(key, out previous);
                    if ((!seen && lengths.Count > 0) || value.Length < previous || value.Length / 8 > previous / 8)
                        samples.Add(new ComposerSample { timestamp = DateTime.UtcNow.ToString("o"), value = value, runtime_id = id });
                    lengths[key] = value.Length;
                }
            }
            catch (Exception) { lock (samples) errors++; }
        }

        public ComposerSample[] GetSamples()
        {
            lock (samples) return samples.ToArray();
        }

        public int ErrorCount { get { lock (samples) return errors; } }

        public void Dispose()
        {
            foreach (var root in roots)
            {
                try { Automation.RemoveAutomationPropertyChangedEventHandler(root, handler); }
                catch (Exception) { lock (samples) errors++; }
            }
        }
    }
}
