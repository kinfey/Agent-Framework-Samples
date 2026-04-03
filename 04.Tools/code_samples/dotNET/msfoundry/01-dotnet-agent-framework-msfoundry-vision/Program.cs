using System;
using Azure.AI.Projects;
using Azure.Identity;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using DotNetEnv;

Env.Load("../../../../../.env");


var endpoint = Environment.GetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT") ?? throw new InvalidOperationException("FOUNDRY_PROJECT_ENDPOINT is not set.");
var deploymentName = Environment.GetEnvironmentVariable("FOUNDRY_MODEL") ?? "gpt-4o-mini";

var imgPath = "../../../files/home.png";

const string AgentName = "Vision-Agent";
const string AgentInstructions = "You are my furniture sales consultant, you can find different furniture elements from the pictures and give me a purchase suggestion";

// WARNING: DefaultAzureCredential is convenient for development but requires careful consideration in production.
// In production, consider using a specific credential (e.g., ManagedIdentityCredential) to avoid
// latency issues, unintended credential probing, and potential security risks from fallback mechanisms.
AIProjectClient aiProjectClient = new(new Uri(endpoint), new DefaultAzureCredential());

AIAgent agent = aiProjectClient.AsAIAgent(
    deploymentName,
    instructions: AgentInstructions,
    name: AgentName);

ChatMessage message = new(ChatRole.User, [
    new TextContent("Can you identify the furniture items in this image and suggest which ones would fit well in a modern living room?"),
    await DataContent.LoadFromAsync(imgPath),
]);

AgentSession session = await agent.CreateSessionAsync();

await foreach (AgentResponseUpdate update in agent.RunStreamingAsync(message, session))
{
    Console.Write(update);
}
Console.WriteLine();

