#pragma warning disable MEAI001 // Type is for evaluation purposes only and is subject to change or removal in future updates
#pragma warning disable OPENAI001 // Type is for evaluation purposes only and is subject to change or removal in future updates

using Azure.AI.Projects;
using Azure.AI.Projects.Agents;
using Azure.Identity;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using OpenAI.Responses;
using DotNetEnv;

Env.Load("../../../../../.env");


var endpoint = Environment.GetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT") ?? throw new InvalidOperationException("FOUNDRY_PROJECT_ENDPOINT is not set.");
var deploymentName = Environment.GetEnvironmentVariable("FOUNDRY_MODEL") ?? "gpt-4o-mini";

// Get a client to create/retrieve server side agents with.
// WARNING: DefaultAzureCredential is convenient for development but requires careful consideration in production.
// In production, consider using a specific credential (e.g., ManagedIdentityCredential) to avoid
// latency issues, unintended credential probing, and potential security risks from fallback mechanisms.
var aiProjectClient = new AIProjectClient(new Uri(endpoint), new DefaultAzureCredential());

// **** MCP Tool with Approval Required ****
// *****************************************

// Create an MCP tool definition that the agent can use.
// In this case we require approval before the tool can be called.
var mcpToolWithApproval = ResponseTool.CreateMcpTool(
    serverLabel: "microsoft_learn",
    serverUri: new Uri("https://learn.microsoft.com/api/mcp"),
    allowedTools: new McpToolFilter() { ToolNames = { "microsoft_docs_search" } },
    toolCallApprovalPolicy: new McpToolCallApprovalPolicy(GlobalMcpToolCallApprovalPolicy.AlwaysRequireApproval));

// Create an agent with the MCP tool that requires approval.
ProjectsAgentVersion agentVersionWithApproval = await aiProjectClient.AgentAdministrationClient.CreateAgentVersionAsync(
    "MicrosoftLearnAgentWithApproval",
    new ProjectsAgentVersionCreationOptions(
        new DeclarativeAgentDefinition(model: deploymentName)
        {
            Instructions = "You answer questions by searching the Microsoft Learn content only.",
            Tools = { mcpToolWithApproval }
        }));

AIAgent agentWithRequiredApproval = aiProjectClient.AsAIAgent(agentVersionWithApproval);

// You can then invoke the agent like any other AIAgent.
// For simplicity, we are assuming here that only mcp tool approvals are pending.
AgentSession sessionWithRequiredApproval = await agentWithRequiredApproval.CreateSessionAsync();
AgentResponse response = await agentWithRequiredApproval.RunAsync("Please summarize the Azure AI Agent documentation related to MCP Tool calling?", sessionWithRequiredApproval);
List<ToolApprovalRequestContent> approvalRequests = response.Messages.SelectMany(m => m.Contents).OfType<ToolApprovalRequestContent>().ToList();

while (approvalRequests.Count > 0)
{
    // Ask the user to approve each MCP call request.
    List<ChatMessage> userInputResponses = approvalRequests
        .ConvertAll(approvalRequest =>
        {
            McpServerToolCallContent mcpToolCall = (McpServerToolCallContent)approvalRequest.ToolCall!;
            Console.WriteLine($"""
                The agent would like to invoke the following MCP Tool, please reply Y to approve.
                ServerName: {mcpToolCall.ServerName}
                Name: {mcpToolCall.Name}
                Arguments: {string.Join(", ", mcpToolCall.Arguments?.Select(x => $"{x.Key}: {x.Value}") ?? [])}
                """);
            return new ChatMessage(ChatRole.User, [approvalRequest.CreateResponse(Console.ReadLine()?.Equals("Y", StringComparison.OrdinalIgnoreCase) ?? false)]);
        });

    // Pass the user input responses back to the agent for further processing.
    response = await agentWithRequiredApproval.RunAsync(userInputResponses, sessionWithRequiredApproval);

    approvalRequests = response.Messages.SelectMany(m => m.Contents).OfType<ToolApprovalRequestContent>().ToList();
}

Console.WriteLine($"\nAgent: {response}");
