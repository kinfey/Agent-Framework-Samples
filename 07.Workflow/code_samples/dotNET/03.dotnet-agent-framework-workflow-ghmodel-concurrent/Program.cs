using System;
using System.ComponentModel;
using System.ClientModel;
using OpenAI;
using Microsoft.Extensions.AI;
using Microsoft.Agents.AI;
using Microsoft.Agents.AI.Workflows;
using DotNetEnv;

// Load environment variables
Env.Load("../../../../.env");

var github_endpoint = Environment.GetEnvironmentVariable("GITHUB_ENDPOINT") ?? throw new InvalidOperationException("GITHUB_ENDPOINT is not set.");
var github_model_id = "gpt-4o";
var github_token = Environment.GetEnvironmentVariable("GITHUB_TOKEN") ?? throw new InvalidOperationException("GITHUB_TOKEN is not set.");

// Configure OpenAI client
var openAIOptions = new OpenAIClientOptions()
{
    Endpoint = new Uri(github_endpoint)
};

var openAIClient = new OpenAIClient(new ApiKeyCredential(github_token), openAIOptions);

// Define agent names and instructions
const string ResearcherAgentName = "Researcher-Agent";
const string ResearcherAgentInstructions = "You are my travel researcher, working with me to analyze the destination, list relevant attractions, and make detailed plans for each attraction.";

const string PlanAgentName = "Plan-Agent";
const string PlanAgentInstructions = "You are my travel planner, working with me to create a detailed travel plan based on the researcher's findings.";

// Create AI agents
var chatClient = openAIClient.GetChatClient(github_model_id).AsIChatClient();

ChatClientAgent researcherAgent = new(
    chatClient,
    name: ResearcherAgentName,
    instructions: ResearcherAgentInstructions);

ChatClientAgent plannerAgent = new(
    chatClient,
    name: PlanAgentName,
    instructions: PlanAgentInstructions);

// Create concurrent executors
var startExecutor = new ConcurrentStartExecutor();
var aggregationExecutor = new ConcurrentAggregationExecutor();

// Build concurrent workflow with FanOut/FanIn pattern
var workflow = new WorkflowBuilder(startExecutor)
            .AddFanOutEdge(startExecutor, [researcherAgent, plannerAgent])
            .AddFanInBarrierEdge([researcherAgent, plannerAgent], aggregationExecutor)
            .WithOutputFrom(aggregationExecutor)
            .Build();

// Execute workflow
await using StreamingRun run = await InProcessExecution.RunStreamingAsync(workflow, input: "Plan a trip to Seattle in December");

string messageData = "";
int eventCount = 0;
await foreach (WorkflowEvent evt in run.WatchStreamAsync())
{
    eventCount++;
    Console.WriteLine($"[Event {eventCount}] Type: {evt.GetType().Name}");
    
    if (evt is WorkflowOutputEvent output)
    {
        messageData = output.Data?.ToString() ?? "";
        Console.WriteLine($"Workflow completed with results:\n{output.Data}");
    }
    else
    {
        Console.WriteLine($"  Event details: {evt}");
    }
}

Console.WriteLine($"\n=== Total Events Received: {eventCount} ===");
Console.WriteLine("\n=== Final Output ===");
Console.WriteLine(string.IsNullOrEmpty(messageData) ? "(No output data)" : messageData);

// Mermaid
Console.WriteLine("\nMermaid string: \n=======");
var mermaid = workflow.ToMermaidString();
Console.WriteLine(mermaid);
Console.WriteLine("=======");

// DOT - Save to file instead of stdout to avoid pipe issues
var dotString = workflow.ToDotString();
var dotFilePath = "workflow.dot";
File.WriteAllText(dotFilePath, dotString);
Console.WriteLine($"\nDOT graph saved to: {dotFilePath}");
Console.WriteLine("To generate image: dot -Tsvg workflow.dot -o workflow.svg");
Console.WriteLine("                   dot -Tpng workflow.dot -o workflow.png");

/// <summary>
/// Executor that starts the concurrent processing by broadcasting messages to all agents.
/// </summary>
[SendsMessage(typeof(ChatMessage))]
[SendsMessage(typeof(TurnToken))]
internal sealed partial class ConcurrentStartExecutor() :
    Executor("ConcurrentStartExecutor")
{
    [MessageHandler]
    public async ValueTask HandleAsync(string message, IWorkflowContext context, CancellationToken cancellationToken = default)
    {
        // Broadcast the message to all connected agents. Receiving agents will queue
        // the message but will not start processing until they receive a turn token.
        await context.SendMessageAsync(new ChatMessage(ChatRole.User, message), cancellationToken: cancellationToken);
        // Broadcast the turn token to kick off the agents.
        await context.SendMessageAsync(new TurnToken(emitEvents: true), cancellationToken: cancellationToken);
    }
}

/// <summary>
/// Executor that aggregates the results from the concurrent agents.
/// </summary>
[YieldsOutput(typeof(string))]
internal sealed partial class ConcurrentAggregationExecutor() :
    Executor<List<ChatMessage>>("ConcurrentAggregationExecutor")
{
    private readonly List<ChatMessage> _messages = [];

    public override async ValueTask HandleAsync(List<ChatMessage> message, IWorkflowContext context, CancellationToken cancellationToken = default)
    {
        Console.WriteLine($"[ConcurrentAggregationExecutor] Received {message.Count} messages");
        this._messages.AddRange(message);
        Console.WriteLine($"[ConcurrentAggregationExecutor] Total messages collected: {this._messages.Count}");

        if (this._messages.Count >= 2)
        {
            var formattedMessages = string.Join(Environment.NewLine, this._messages.Select(m => $"{m.AuthorName}: {m.Text}"));
            Console.WriteLine($"[ConcurrentAggregationExecutor] Yielding output with {this._messages.Count} messages");
            await context.YieldOutputAsync(formattedMessages, cancellationToken);
        }
        else
        {
            Console.WriteLine($"[ConcurrentAggregationExecutor] Waiting for more messages (need 2, have {this._messages.Count})");
        }
    }
}
